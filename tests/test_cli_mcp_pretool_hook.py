"""Tests for the PreToolUse deference hook + its install/uninstall wiring.

Covers:
  - the pure classifier (classify_pretool_event) across WebFetch/WebSearch/Bash
  - run_pretool_hook stdin → (exit, stdout, stderr) incl. block + fail-open
  - the `aztea mcp pretool-hook` command (stdin fixtures)
  - install wiring: all three Claude hooks coexist, --pretool-block escalates,
    the strict-parse guard skips hook wiring without clobbering settings.json,
    non-Claude clients get no hooks, uninstall removes everything.
"""
from __future__ import annotations

import dataclasses
import io
import json

import pytest
import typer

from aztea.cli import mcp, mcp_hooks


# ── Fakes / fixtures ───────────────────────────────────────────────────────

class _FakeAuth:
    def me(self) -> dict:
        return {"username": "tester"}


class _FakeClient:
    auth = _FakeAuth()

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture
def claude_env(monkeypatch, tmp_path):
    """Point Claude's three write targets (mcp.json, CLAUDE.md, settings.json)
    at tmp and stub auth so install() reaches the hook-write step."""
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    claude_json = tmp_path / ".claude.json"
    monkeypatch.setattr(mcp, "_CLAUDE_SETTINGS_PATH", settings)
    monkeypatch.setattr(mcp, "_CLAUDE_MD_PATH", claude_md)
    patched = dict(mcp._TARGETS)
    patched["claude"] = dataclasses.replace(patched["claude"], config_path=claude_json)
    monkeypatch.setattr(mcp, "_TARGETS", patched)
    monkeypatch.setattr(
        mcp, "load_config",
        lambda: {"api_key": "az_test", "base_url": "https://aztea.ai"},
    )
    monkeypatch.setattr(mcp, "build_client", lambda **_: _FakeClient())
    return {"settings": settings, "claude_md": claude_md, "claude_json": claude_json}


def _hook_commands(settings: dict, event: str) -> list[str]:
    return [
        str(h.get("command") or "")
        for entry in settings.get("hooks", {}).get(event, [])
        for h in entry.get("hooks", [])
    ]


# ── classify_pretool_event (pure) ──────────────────────────────────────────

@pytest.mark.parametrize("tool", ["WebFetch", "WebSearch"])
def test_classify_web_tools_warn_by_default(tool):
    d = mcp_hooks.classify_pretool_event({"tool_name": tool}, allow_block=False)
    assert d is not None and d.action == "warn" and d.category == "web"


@pytest.mark.parametrize("tool", ["WebFetch", "WebSearch"])
def test_classify_web_tools_block_when_allowed(tool):
    d = mcp_hooks.classify_pretool_event({"tool_name": tool}, allow_block=True)
    assert d is not None and d.action == "block"


def test_classify_bash_network_install_exec_warn():
    def cat(cmd):
        d = mcp_hooks.classify_pretool_event(
            {"tool_name": "Bash", "tool_input": {"command": cmd}}, allow_block=True
        )
        return None if d is None else (d.action, d.category)

    assert cat("curl https://example.com") == ("warn", "live_data")
    assert cat("wget http://x/y") == ("warn", "live_data")
    assert cat("pip install requests") == ("warn", "deps")
    assert cat("npm install left-pad") == ("warn", "deps")
    assert cat("python -c 'print(1)'") == ("warn", "exec")
    assert cat("node -e 'console.log(1)'") == ("warn", "exec")
    # Bash never blocks even under allow_block.
    assert cat("curl https://x")[0] == "warn"


def test_classify_bash_safe_and_edge_cases_silent():
    for cmd in ("ls -la", "git status", "cat README.md", "make test", "pytest -q", ""):
        assert mcp_hooks.classify_pretool_event(
            {"tool_name": "Bash", "tool_input": {"command": cmd}}, allow_block=False
        ) is None
    assert mcp_hooks.classify_pretool_event({"tool_name": "Edit"}, allow_block=False) is None
    assert mcp_hooks.classify_pretool_event("not a dict", allow_block=False) is None


def test_classify_tolerates_camelcase_keys():
    d = mcp_hooks.classify_pretool_event(
        {"toolName": "Bash", "toolInput": {"command": "pip install x"}}, allow_block=False
    )
    assert d is not None and d.category == "deps"


# ── run_pretool_hook (pure: stdin text -> exit/stdout/stderr) ───────────────

def test_run_pretool_warn_web():
    code, out, err = mcp_hooks.run_pretool_hook(
        json.dumps({"tool_name": "WebFetch", "tool_input": {"url": "https://x"}}), mode="warn"
    )
    assert code == 0 and out == ""
    assert mcp_hooks.PRETOOL_MARKER in err and "auto_call_agent" in err


def test_run_pretool_block_web_emits_deny_and_exit_2():
    code, out, err = mcp_hooks.run_pretool_hook(
        json.dumps({"tool_name": "WebSearch", "tool_input": {"query": "x"}}), mode="block"
    )
    assert code == 2
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "auto_call_agent" in err


def test_run_pretool_bash_never_blocks_even_in_block_mode():
    code, _out, err = mcp_hooks.run_pretool_hook(
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "curl https://x"}}),
        mode="block",
    )
    assert code == 0 and mcp_hooks.PRETOOL_MARKER in err


def test_run_pretool_fail_open_on_bad_stdin_and_silent_tools():
    assert mcp_hooks.run_pretool_hook("{not json", mode="warn") == (0, "", "")
    assert mcp_hooks.run_pretool_hook("", mode="warn") == (0, "", "")
    assert mcp_hooks.run_pretool_hook(
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}), mode="warn"
    ) == (0, "", "")


# ── `aztea mcp pretool-hook` command (stdin) ───────────────────────────────

def _run_command(monkeypatch, stdin_text, mode):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    with pytest.raises(typer.Exit) as excinfo:
        mcp.pretool_hook(mode=mode)
    return int(excinfo.value.exit_code or 0)


def test_command_warn_exits_0_with_stderr(monkeypatch, capsys):
    code = _run_command(
        monkeypatch, json.dumps({"tool_name": "WebFetch", "tool_input": {"url": "https://x"}}), "warn"
    )
    captured = capsys.readouterr()
    assert code == 0
    assert mcp_hooks.PRETOOL_MARKER in captured.err


def test_command_block_exits_2(monkeypatch, capsys):
    code = _run_command(
        monkeypatch, json.dumps({"tool_name": "WebFetch", "tool_input": {"url": "https://x"}}), "block"
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "deny" in captured.out


def test_command_fail_open_on_garbage(monkeypatch):
    assert _run_command(monkeypatch, "garbage", "warn") == 0


# ── install / uninstall wiring (Claude only) ───────────────────────────────

def test_install_claude_wires_all_three_hooks(claude_env):
    mcp.install(client="claude", api_key=None, base_url=None, json_mode=True)
    settings = json.loads(claude_env["settings"].read_text())
    assert any("aztea mcp pretool-hook" in c for c in _hook_commands(settings, "PreToolUse"))
    assert any("aztea mcp prompt-hook" in c for c in _hook_commands(settings, "UserPromptSubmit"))
    assert any(mcp._HOOK_MARKER in c for c in _hook_commands(settings, "PostToolUse"))
    assert mcp._REFLEX_RULE_BEGIN in claude_env["claude_md"].read_text()
    # PreToolUse default is warn (no --mode block).
    assert not any("--mode block" in c for c in _hook_commands(settings, "PreToolUse"))


def test_install_pretool_block_writes_block_command(claude_env):
    mcp.install(
        client="claude", api_key=None, base_url=None, pretool_block=True, json_mode=True
    )
    settings = json.loads(claude_env["settings"].read_text())
    assert any("aztea mcp pretool-hook --mode block" in c for c in _hook_commands(settings, "PreToolUse"))


def test_install_skips_hooks_on_unparseable_settings(claude_env):
    """Strict guard: a JSONC settings.json must not be clobbered; MCP still
    registers, hooks are skipped."""
    original = '{\n  // a comment makes this invalid JSON\n  "theme": "dark"\n}\n'
    claude_env["settings"].write_text(original, encoding="utf-8")
    mcp.install(client="claude", api_key=None, base_url=None, json_mode=True)
    assert "aztea" in json.loads(claude_env["claude_json"].read_text())["mcpServers"]
    assert claude_env["settings"].read_text() == original  # untouched


def test_uninstall_claude_removes_all_hooks(claude_env):
    mcp.install(client="claude", api_key=None, base_url=None, json_mode=True)
    mcp.uninstall(client="claude", json_mode=True)
    blob = claude_env["settings"].read_text() if claude_env["settings"].exists() else "{}"
    assert "aztea mcp pretool-hook" not in blob
    assert "aztea mcp prompt-hook" not in blob
    assert mcp._HOOK_MARKER not in blob
    md = claude_env["claude_md"].read_text() if claude_env["claude_md"].exists() else ""
    assert mcp._REFLEX_RULE_BEGIN not in md


def test_install_non_claude_writes_no_hooks(monkeypatch, tmp_path, claude_env):
    cursor_json = tmp_path / "cursor.json"
    patched = dict(mcp._TARGETS)
    patched["cursor"] = dataclasses.replace(patched["cursor"], config_path=cursor_json)
    monkeypatch.setattr(mcp, "_TARGETS", patched)

    mcp.install(client="cursor", api_key=None, base_url=None, json_mode=True)
    # Cursor MCP entry written; no Claude hooks/rule touched.
    assert "aztea" in json.loads(cursor_json.read_text())["mcpServers"]
    assert not claude_env["settings"].exists()
    assert not claude_env["claude_md"].exists()


def test_pretool_hook_install_is_idempotent(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(mcp, "_CLAUDE_SETTINGS_PATH", settings)
    assert mcp_hooks.write_pretool_hook(block=False) is True
    assert mcp_hooks.write_pretool_hook(block=False) is False
    assert mcp_hooks.has_pretool_hook() is True
    assert mcp_hooks.remove_pretool_hook() is True
    assert mcp_hooks.remove_pretool_hook() is False
    assert mcp_hooks.has_pretool_hook() is False


def test_classify_bash_install_pipe_and_exec_variants():
    def cat(cmd):
        d = mcp_hooks.classify_pretool_event(
            {"tool_name": "Bash", "tool_input": {"command": cmd}}, allow_block=True
        )
        return None if d is None else (d.action, d.category)

    # curl | sh: network regex is checked first, so it classifies as live_data
    assert cat("curl https://get.example.com | sh") == ("warn", "live_data")
    assert cat("npx create-vite app") == ("warn", "deps")
    assert cat("brew install ripgrep") == ("warn", "deps")
    assert cat("cargo install ripgrep") == ("warn", "deps")
    assert cat("bash -c 'echo hi'") == ("warn", "exec")
    # `deno run` has no -c/-e, so it is NOT ad-hoc exec — stays silent.
    assert cat("deno run mod.ts") is None


def test_write_hook_refuses_non_dict_hooks_node(monkeypatch, tmp_path):
    """Distinct from the JSON-parse guard: valid JSON whose `hooks` is the wrong
    type must not be clobbered."""
    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks": "enabled"}', encoding="utf-8")
    monkeypatch.setattr(mcp, "_CLAUDE_SETTINGS_PATH", settings)
    original = settings.read_text()
    assert mcp_hooks.write_pretool_hook(block=False) is False
    assert settings.read_text() == original


def test_remove_hook_on_missing_file_is_false(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, "_CLAUDE_SETTINGS_PATH", tmp_path / "does-not-exist.json")
    assert mcp_hooks.remove_pretool_hook() is False
    assert mcp_hooks.remove_prompt_hook() is False


def test_doctor_hook_rows_are_informational_not_gating(claude_env, capsys):
    """The two Claude hook rows report state but never fail `doctor` health."""
    mcp.install(client="claude", api_key=None, base_url=None, json_mode=True)
    capsys.readouterr()  # drain install output
    mcp.doctor(client="claude", json_mode=True)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    rows = {c["name"]: c["ok"] for c in out["checks"]}
    pre = next(n for n in rows if n.startswith("PreToolUse deference hook"))
    scout = next(n for n in rows if n.startswith("UserPromptSubmit scout hook"))
    assert "active" in pre and rows[pre] is True
    assert "active" in scout and rows[scout] is True
