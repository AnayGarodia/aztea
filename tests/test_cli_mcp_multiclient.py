"""Unit + command tests for multi-client MCP install (`aztea mcp install`).

Covers the runtime-agnostic surface added so Aztea registers in any MCP
client, not just Claude Code:

  - JSON clients with differing nested key paths (Cursor/Windsurf use
    ``mcpServers``; VS Code nests under ``mcp.servers``).
  - Codex (TOML) via a marker-fenced block — validated by round-tripping
    through ``tomllib``.
  - The strict-parse guard that refuses to overwrite a config we can't
    parse (e.g. a VS Code settings.json carrying JSONC comments).
"""
from __future__ import annotations

import dataclasses
import json

import pytest
import typer

from aztea.cli import mcp


# ── Fakes for the install command's network + config dependencies ──────────

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
def install_env(monkeypatch):
    """Stub out auth + saved config so `install` reaches the write step."""
    monkeypatch.setattr(
        mcp, "load_config",
        lambda: {"api_key": "az_test_key", "base_url": "https://aztea.ai"},
    )
    monkeypatch.setattr(mcp, "build_client", lambda **_: _FakeClient())


def _point_target_at(monkeypatch, client: str, path) -> None:
    """Redirect one client's config to a tmp path (dataclass is frozen)."""
    patched = dict(mcp._TARGETS)
    patched[client] = dataclasses.replace(patched[client], config_path=path)
    monkeypatch.setattr(mcp, "_TARGETS", patched)


# ── _nested_servers / _prune_empty_path ────────────────────────────────────

def test_nested_servers_creates_vscode_path() -> None:
    data: dict = {}
    servers = mcp._nested_servers(data, ("mcp", "servers"), create=True)
    assert servers == {}
    servers["aztea"] = {"x": 1}
    assert data == {"mcp": {"servers": {"aztea": {"x": 1}}}}


def test_nested_servers_missing_returns_none_without_create() -> None:
    assert mcp._nested_servers({}, ("mcp", "servers"), create=False) is None
    assert mcp._nested_servers({"mcp": 5}, ("mcp", "servers"), create=False) is None


def test_prune_empty_path_removes_scaffolding() -> None:
    data = {"mcp": {"servers": {}}, "keep": 1}
    mcp._prune_empty_path(data, ("mcp", "servers"))
    assert data == {"keep": 1}


# ── _read_config_or_raise (strict guard) ───────────────────────────────────

def test_read_config_or_raise_empty_and_missing(tmp_path) -> None:
    missing = tmp_path / "nope.json"
    assert mcp._read_config_or_raise(missing) == {}
    empty = tmp_path / "empty.json"
    empty.write_text("   \n", encoding="utf-8")
    assert mcp._read_config_or_raise(empty) == {}


def test_read_config_or_raise_rejects_unparseable(tmp_path) -> None:
    bad = tmp_path / "settings.json"
    bad.write_text('{\n  // a JSONC comment\n  "a": 1\n}\n', encoding="utf-8")
    with pytest.raises(mcp._ConfigParseError):
        mcp._read_config_or_raise(bad)


# ── Codex TOML block ───────────────────────────────────────────────────────

def _parse_toml(path):
    import tomllib  # py3.11+; CI + dev run 3.11+

    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_codex_write_produces_valid_toml(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    assert mcp._codex_write_entry(cfg, "az_secret", "https://aztea.ai") is True
    parsed = _parse_toml(cfg)
    entry = parsed["mcp_servers"]["aztea"]
    assert entry["command"] == "aztea"
    assert entry["args"] == ["mcp", "serve"]
    assert entry["env"]["AZTEA_API_KEY"] == "az_secret"
    assert entry["env"]["AZTEA_BASE_URL"] == "https://aztea.ai"


def test_codex_write_is_idempotent_and_updates(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    assert mcp._codex_write_entry(cfg, "az_a", "https://aztea.ai") is True
    # Same values → no change.
    assert mcp._codex_write_entry(cfg, "az_a", "https://aztea.ai") is False
    # Changed value → rewrite, still single entry.
    assert mcp._codex_write_entry(cfg, "az_b", "https://aztea.ai") is True
    assert cfg.read_text().count(mcp._CODEX_BEGIN) == 1
    assert _parse_toml(cfg)["mcp_servers"]["aztea"]["env"]["AZTEA_API_KEY"] == "az_b"


def test_codex_block_preserves_surrounding_config(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "o3"\n\n[other]\nkey = "val"\n', encoding="utf-8")
    mcp._codex_write_entry(cfg, "az_x", "https://aztea.ai")
    parsed = _parse_toml(cfg)
    assert parsed["model"] == "o3"
    assert parsed["other"]["key"] == "val"
    assert parsed["mcp_servers"]["aztea"]["command"] == "aztea"


def test_codex_remove_restores_and_extract_env(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "o3"\n', encoding="utf-8")
    mcp._codex_write_entry(cfg, "az_q", "https://aztea.ai")
    assert mcp._codex_extract_env(cfg) == {
        "AZTEA_API_KEY": "az_q",
        "AZTEA_BASE_URL": "https://aztea.ai",
    }
    assert mcp._codex_remove_entry(cfg) is True
    assert mcp._CODEX_BEGIN not in cfg.read_text()
    assert _parse_toml(cfg) == {"model": "o3"}
    # Idempotent: removing again is a no-op.
    assert mcp._codex_remove_entry(cfg) is False


def test_codex_write_replaces_truncated_fence(tmp_path) -> None:
    """A BEGIN with no END (corrupted/half-written) is replaced cleanly, not
    duplicated, and surrounding config survives."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'model = "o3"\n{mcp._CODEX_BEGIN}\n[mcp_servers.aztea]\ncommand = "old"\n',
        encoding="utf-8",
    )
    assert mcp._codex_write_entry(cfg, "az_k", "https://aztea.ai") is True
    text = cfg.read_text()
    assert text.count(mcp._CODEX_BEGIN) == 1 and text.count(mcp._CODEX_END) == 1
    parsed = _parse_toml(cfg)
    assert parsed["model"] == "o3"
    assert parsed["mcp_servers"]["aztea"]["command"] == "aztea"


def test_codex_remove_strips_truncated_fence(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'model = "o3"\n{mcp._CODEX_BEGIN}\ndangling no end\n', encoding="utf-8")
    assert mcp._codex_remove_entry(cfg) is True
    assert mcp._CODEX_BEGIN not in cfg.read_text()
    assert _parse_toml(cfg) == {"model": "o3"}


def test_codex_write_refuses_to_corrupt_invalid_surrounding_toml(tmp_path) -> None:
    """Fail loud, don't corrupt: if appending the block would yield invalid
    TOML (pre-broken surrounding config), refuse and leave the file untouched."""
    cfg = tmp_path / "config.toml"
    broken = "this is not valid toml [[[\n"
    cfg.write_text(broken, encoding="utf-8")
    with pytest.raises(ValueError):
        mcp._codex_write_entry(cfg, "az_k", "https://aztea.ai")
    assert cfg.read_text() == broken  # untouched


def test_codex_write_escapes_control_chars_and_round_trips(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    mcp._codex_write_entry(cfg, "az\nkey\ttab", "https://aztea.ai")
    env = _parse_toml(cfg)["mcp_servers"]["aztea"]["env"]
    assert env["AZTEA_API_KEY"] == "az\nkey\ttab"  # escaped on write, decoded back


def test_codex_write_sets_owner_only_perms(tmp_path) -> None:
    import stat
    cfg = tmp_path / "config.toml"
    mcp._codex_write_entry(cfg, "az_k", "https://aztea.ai")
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


# ── install command: per-client write dispatch ─────────────────────────────

def test_install_vscode_writes_nested_key(monkeypatch, tmp_path, install_env) -> None:
    cfg = tmp_path / "settings.json"
    cfg.write_text('{"editor.fontSize": 13}\n', encoding="utf-8")
    _point_target_at(monkeypatch, "vscode", cfg)

    mcp.install(client="vscode", api_key=None, base_url=None, json_mode=True)

    data = json.loads(cfg.read_text())
    assert data["editor.fontSize"] == 13  # existing settings preserved
    entry = data["mcp"]["servers"]["aztea"]
    assert entry["command"] == "aztea"
    assert entry["env"]["AZTEA_API_KEY"] == "az_test_key"


def test_install_codex_writes_toml(monkeypatch, tmp_path, install_env) -> None:
    cfg = tmp_path / "config.toml"
    _point_target_at(monkeypatch, "codex", cfg)

    mcp.install(client="codex", api_key=None, base_url=None, json_mode=True)

    assert mcp._codex_has_entry(cfg)
    assert _parse_toml(cfg)["mcp_servers"]["aztea"]["command"] == "aztea"


def test_install_aborts_on_unparseable_config(monkeypatch, tmp_path, install_env) -> None:
    """The strict guard: never clobber a config we can't parse."""
    cfg = tmp_path / "settings.json"
    original = '{\n  // comment makes this invalid JSON\n  "editor.fontSize": 13\n}\n'
    cfg.write_text(original, encoding="utf-8")
    _point_target_at(monkeypatch, "vscode", cfg)

    with pytest.raises(typer.Exit):
        mcp.install(client="vscode", api_key=None, base_url=None, json_mode=True)

    assert cfg.read_text() == original  # untouched


# ── uninstall command: per-client removal ──────────────────────────────────

def test_uninstall_vscode_prunes_nested_scaffolding(monkeypatch, tmp_path, install_env) -> None:
    cfg = tmp_path / "settings.json"
    cfg.write_text('{"editor.fontSize": 13}\n', encoding="utf-8")
    _point_target_at(monkeypatch, "vscode", cfg)

    mcp.install(client="vscode", api_key=None, base_url=None, json_mode=True)
    mcp.uninstall(client="vscode", json_mode=True)

    data = json.loads(cfg.read_text())
    assert "mcp" not in data  # empty {"mcp": {"servers": {}}} pruned away
    assert data["editor.fontSize"] == 13


# ── is_mcp_registered across formats ───────────────────────────────────────

def test_is_mcp_registered_vscode_and_codex(monkeypatch, tmp_path, install_env) -> None:
    vs = tmp_path / "settings.json"
    cx = tmp_path / "config.toml"
    _point_target_at(monkeypatch, "vscode", vs)
    _point_target_at(monkeypatch, "codex", cx)

    assert mcp.is_mcp_registered("vscode") is False
    assert mcp.is_mcp_registered("codex") is False

    mcp.install(client="vscode", api_key=None, base_url=None, json_mode=True)
    mcp.install(client="codex", api_key=None, base_url=None, json_mode=True)

    assert mcp.is_mcp_registered("vscode") is True
    assert mcp.is_mcp_registered("codex") is True
    assert mcp.is_mcp_registered("unknown-client") is False


def test_install_writes_json_config_owner_only(monkeypatch, tmp_path, install_env) -> None:
    """The MCP config embeds the API key — it must be written 0600, not 0644."""
    import stat
    cfg = tmp_path / "mcp.json"
    _point_target_at(monkeypatch, "cursor", cfg)
    mcp.install(client="cursor", api_key=None, base_url=None, json_mode=True)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


# ── client_id attribution (agent-harness surfaces) ─────────────────────────

def test_server_entry_omits_client_id_by_default() -> None:
    """Byte-identical to the pre-change shape — editors must not regress."""
    assert mcp._server_entry("az_k", "https://aztea.ai") == {
        "type": "stdio",
        "command": "aztea",
        "args": ["mcp", "serve"],
        "env": {"AZTEA_API_KEY": "az_k", "AZTEA_BASE_URL": "https://aztea.ai"},
    }


def test_server_entry_stamps_client_id_when_given() -> None:
    entry = mcp._server_entry("az_k", "https://aztea.ai", client_id="openclaw")
    assert entry["env"]["AZTEA_CLIENT_ID"] == "openclaw"


def test_attribution_only_tags_harness_surfaces() -> None:
    assert mcp._attribution_client_id("openclaw") == "openclaw"
    assert mcp._attribution_client_id("hermes") == "hermes"
    assert mcp._attribution_client_id("claude") is None
    assert mcp._attribution_client_id("cursor") is None


# ── OpenClaw (JSON, nested mcp.servers) ────────────────────────────────────

def test_install_openclaw_nested_mcp_servers_with_attribution(monkeypatch, tmp_path, install_env) -> None:
    cfg = tmp_path / "openclaw.json"
    cfg.write_text('{"model": "x"}\n', encoding="utf-8")
    _point_target_at(monkeypatch, "openclaw", cfg)
    mcp.install(client="openclaw", api_key=None, base_url=None, json_mode=True)
    data = json.loads(cfg.read_text())
    assert data["model"] == "x"  # surrounding config preserved
    entry = data["mcp"]["servers"]["aztea"]  # NESTED, not top-level mcpServers
    assert entry["command"] == "aztea" and entry["args"] == ["mcp", "serve"]
    assert entry["env"]["AZTEA_API_KEY"] == "az_test_key"
    assert entry["env"]["AZTEA_CLIENT_ID"] == "openclaw"


def test_uninstall_openclaw_prunes_nested(monkeypatch, tmp_path, install_env) -> None:
    cfg = tmp_path / "openclaw.json"
    cfg.write_text('{"model": "x"}\n', encoding="utf-8")
    _point_target_at(monkeypatch, "openclaw", cfg)
    mcp.install(client="openclaw", api_key=None, base_url=None, json_mode=True)
    mcp.uninstall(client="openclaw", json_mode=True)
    data = json.loads(cfg.read_text())
    assert "mcp" not in data and data["model"] == "x"


# ── Hermes (YAML, top-level mcp_servers) ───────────────────────────────────

def test_install_hermes_writes_yaml_mcp_servers(monkeypatch, tmp_path, install_env) -> None:
    import yaml
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model: hermes-4\n", encoding="utf-8")
    _point_target_at(monkeypatch, "hermes", cfg)
    mcp.install(client="hermes", api_key=None, base_url=None, json_mode=True)
    data = yaml.safe_load(cfg.read_text())
    assert data["model"] == "hermes-4"  # surrounding key preserved
    entry = data["mcp_servers"]["aztea"]  # TOP-LEVEL snake_case, YAML
    assert entry["command"] == "aztea" and entry["args"] == ["mcp", "serve"]
    assert entry["env"]["AZTEA_CLIENT_ID"] == "hermes"
    assert entry["env"]["AZTEA_API_KEY"] == "az_test_key"


def test_hermes_write_is_idempotent(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    assert mcp._hermes_write_entry(cfg, "az_a", "https://aztea.ai", client_id="hermes") is True
    assert mcp._hermes_write_entry(cfg, "az_a", "https://aztea.ai", client_id="hermes") is False
    assert mcp._hermes_write_entry(cfg, "az_b", "https://aztea.ai", client_id="hermes") is True


def test_hermes_write_owner_only_perms(tmp_path) -> None:
    import stat
    cfg = tmp_path / "config.yaml"
    mcp._hermes_write_entry(cfg, "az_k", "https://aztea.ai")
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_hermes_refuses_non_mapping_yaml(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mcp._hermes_write_entry(cfg, "az_k", "https://aztea.ai")


def test_hermes_malformed_yaml_raises_value_error_not_yamlerror(tmp_path) -> None:
    # PyYAML raises YAMLError (not ValueError) on bad YAML; the writer must
    # normalize it so the install path's clean refusal catches it.
    cfg = tmp_path / "config.yaml"
    cfg.write_text("key: [unclosed\n  bad: : :\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mcp._hermes_write_entry(cfg, "az_k", "https://aztea.ai")


def test_hermes_refuses_list_form_mcp_servers(tmp_path) -> None:
    # A hand-edited list-form mcp_servers must NOT be silently clobbered.
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mcp_servers:\n  - name: foo\n  - name: bar\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mcp._hermes_write_entry(cfg, "az_k", "https://aztea.ai")
    assert "foo" in cfg.read_text()  # original left untouched


def test_install_hermes_missing_pyyaml_exits_cleanly(monkeypatch, tmp_path, install_env) -> None:
    cfg = tmp_path / "config.yaml"
    _point_target_at(monkeypatch, "hermes", cfg)

    def _raise(*_a, **_k):
        raise mcp.HermesYamlUnavailable("PyYAML not installed")
    monkeypatch.setattr(mcp, "_hermes_write_entry", _raise)
    with pytest.raises(typer.Exit):
        mcp.install(client="hermes", api_key=None, base_url=None, json_mode=True)


def test_install_hermes_parse_error_exits_cleanly(monkeypatch, tmp_path, install_env) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("key: [unclosed\n  : :\n", encoding="utf-8")
    _point_target_at(monkeypatch, "hermes", cfg)
    with pytest.raises(typer.Exit):
        mcp.install(client="hermes", api_key=None, base_url=None, json_mode=True)
    assert "unclosed" in cfg.read_text()  # untouched on refusal


def test_uninstall_hermes_prunes(monkeypatch, tmp_path, install_env) -> None:
    import yaml
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model: hermes-4\n", encoding="utf-8")
    _point_target_at(monkeypatch, "hermes", cfg)
    mcp.install(client="hermes", api_key=None, base_url=None, json_mode=True)
    mcp.uninstall(client="hermes", json_mode=True)
    data = yaml.safe_load(cfg.read_text())
    assert "mcp_servers" not in data and data["model"] == "hermes-4"


def test_is_mcp_registered_openclaw_and_hermes(monkeypatch, tmp_path, install_env) -> None:
    oc = tmp_path / "openclaw.json"
    hm = tmp_path / "config.yaml"
    _point_target_at(monkeypatch, "openclaw", oc)
    _point_target_at(monkeypatch, "hermes", hm)
    assert mcp.is_mcp_registered("openclaw") is False
    assert mcp.is_mcp_registered("hermes") is False
    mcp.install(client="openclaw", api_key=None, base_url=None, json_mode=True)
    mcp.install(client="hermes", api_key=None, base_url=None, json_mode=True)
    assert mcp.is_mcp_registered("openclaw") is True
    assert mcp.is_mcp_registered("hermes") is True
