"""mcp: install / doctor / uninstall the Aztea MCP server in your editor."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import typer

from ..config import load_config
from .common import ApiKeyOpt, BaseUrlOpt, JsonOpt, build_client, handle_error
from .output import (
    BAR,
    CHECK,
    CROSS,
    DOT,
    _HAS_RICH,
    banner,
    console,
    emit,
    info,
    kv_table,
    section,
    spinner,
    success,
    warn,
)


app = typer.Typer(
    help="Install, verify, and remove the Aztea MCP server in your editor.",
    no_args_is_help=True,
)


# ── Client targets ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _ClientTarget:
    name: str
    config_path: Path

    @property
    def label(self) -> str:
        return self.name


def _claude_path() -> Path:
    return Path.home() / ".claude.json"


def _cursor_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def _vscode_path() -> Path:
    base = (
        Path(os.environ.get("APPDATA", Path.home() / ".config"))
        if os.name == "nt"
        else Path.home() / "Library" / "Application Support"
        if os.uname().sysname == "Darwin"
        else Path.home() / ".config"
    )
    return base / "Code" / "User" / "settings.json"


_TARGETS: dict[str, _ClientTarget] = {
    "claude": _ClientTarget("Claude Code", _claude_path()),
    "cursor": _ClientTarget("Cursor",      _cursor_path()),
}


def is_mcp_registered(client: str = "claude") -> bool:
    """Return True iff the Aztea entry is present in the editor's MCP config.

    Used by the REPL banner to decide whether to surface the "/init tip"
    above the Quickstart panel, and by /claude-code before launching to
    warn the user (without blocking) that their just-launched session
    won't have Aztea tools available.

    Safe to call on cold install (missing files, malformed JSON, etc.) —
    any exception surfaces as ``False`` so callers can use the result
    unconditionally.
    """
    try:
        target = _TARGETS.get((client or "").strip().lower())
        if target is None:
            return False
        cfg = _read_config(target.path)
        return "aztea" in (cfg.get("mcpServers") or {})
    except Exception:
        return False


def _resolve_target(client: str) -> _ClientTarget:
    key = (client or "").strip().lower()
    if key not in _TARGETS:
        from .output import error
        error(
            f"Unknown client '{client}'.",
            hint="Try one of: claude, cursor.",
            code="mcp.unknown_client",
        )
        raise typer.Exit(code=1)
    return _TARGETS[key]


def client_label(client: str) -> str:
    """Return the user-facing display name for ``client`` (e.g. "Claude Code").

    Falls back to "your editor" when ``client`` is unknown so messages
    relying on this never end up with a stray empty string. Used by
    ``init.py`` and any other surface that needs to name the client
    without holding a ``_ClientTarget`` ref.
    """
    target = _TARGETS.get((client or "").strip().lower())
    return target.label if target is not None else "your editor"


# ── Config IO ──────────────────────────────────────────────────────────────

def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _write_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _server_entry(api_key: str, base_url: str) -> dict[str, Any]:
    """Standard stdio MCP server entry, launched via the pip-installed `aztea` CLI."""
    return {
        "type": "stdio",
        "command": "aztea",
        "args": ["mcp", "serve"],
        "env": {
            "AZTEA_API_KEY": api_key,
            "AZTEA_BASE_URL": base_url,
        },
    }


# ── Reflex rule + PostToolUse hook (Claude target only) ────────────────────

_CLAUDE_MD_PATH = Path.home() / ".claude" / "CLAUDE.md"
_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_REFLEX_RULE_BEGIN = "<!-- aztea:auto-invoke-rule:begin -->"
_REFLEX_RULE_END = "<!-- aztea:auto-invoke-rule:end -->"
_REFLEX_RULE_BODY = (
    "## Aztea reflex routing\n\n"
    "When responding to prompts about code, config, infrastructure, security, "
    "or live data, call `do_specialist_task(intent=..., dry_run=true)` first. "
    "It's free and returns in under a second. If `would_invoke=true`, re-call "
    "with `dry_run=false` to execute (capped at `max_cost_usd`, default $0.10, "
    "failures refund automatically).\n"
)
# Marker token embedded in the hook command so we can find and remove it
# idempotently in uninstall without parsing every other PostToolUse hook the
# user may have configured.
_HOOK_MARKER = "aztea-auto-invoke-hint"
_HOOK_COMMAND = (
    f"echo '[{_HOOK_MARKER}] Aztea: consider do_specialist_task(dry_run=true)"
    " for security/dep/lint review of this change.' >&2"
)
_HOOK_MATCHER = "Edit|Write|MultiEdit"


def _claude_md_block() -> str:
    """Return the marker-fenced reflex rule block written to CLAUDE.md."""
    return f"{_REFLEX_RULE_BEGIN}\n{_REFLEX_RULE_BODY}{_REFLEX_RULE_END}\n"


def _write_reflex_rule() -> bool:
    """Append the reflex rule to ~/.claude/CLAUDE.md if not already present.

    Idempotent: the marker fence makes second writes a no-op. Returns True
    when the file was modified, False when the rule was already there.
    """
    path = _CLAUDE_MD_PATH
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if _REFLEX_RULE_BEGIN in existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if existing.endswith("\n") or not existing else "\n"
    path.write_text(existing + separator + _claude_md_block(), encoding="utf-8")
    return True


def _remove_reflex_rule() -> bool:
    """Remove the marker-fenced reflex rule from CLAUDE.md. Idempotent."""
    path = _CLAUDE_MD_PATH
    if not path.exists():
        return False
    existing = path.read_text(encoding="utf-8")
    if _REFLEX_RULE_BEGIN not in existing:
        return False
    start = existing.find(_REFLEX_RULE_BEGIN)
    end_marker_idx = existing.find(_REFLEX_RULE_END, start)
    if end_marker_idx < 0:
        return False
    end = end_marker_idx + len(_REFLEX_RULE_END)
    # Strip a trailing newline immediately after the end marker so we don't
    # leave a blank-line scar where the block used to live.
    if end < len(existing) and existing[end] == "\n":
        end += 1
    cleaned = existing[:start] + existing[end:]
    path.write_text(cleaned, encoding="utf-8")
    return True


def _post_tool_hook_entry() -> dict[str, Any]:
    """One PostToolUse hook entry matching Edit/Write/MultiEdit."""
    return {
        "matcher": _HOOK_MATCHER,
        "hooks": [{"type": "command", "command": _HOOK_COMMAND}],
    }


def _settings_has_aztea_hook(settings: dict[str, Any]) -> bool:
    """Return True if our marker command is already wired as a PostToolUse hook."""
    hooks_root = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks_root, dict):
        return False
    post_tool = hooks_root.get("PostToolUse")
    if not isinstance(post_tool, list):
        return False
    for entry in post_tool:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict) and _HOOK_MARKER in str(hook.get("command") or ""):
                return True
    return False


def _write_post_tool_hook() -> bool:
    """Append our reflex-hint PostToolUse hook to ~/.claude/settings.json.

    Idempotent via the marker check. Returns True on modification.
    """
    settings = _read_config(_CLAUDE_SETTINGS_PATH)
    if _settings_has_aztea_hook(settings):
        return False
    hooks_root = settings.setdefault("hooks", {})
    if not isinstance(hooks_root, dict):
        # Existing settings have a non-dict at "hooks" — refuse to clobber.
        return False
    post_tool = hooks_root.setdefault("PostToolUse", [])
    if not isinstance(post_tool, list):
        return False
    post_tool.append(_post_tool_hook_entry())
    _write_config(_CLAUDE_SETTINGS_PATH, settings)
    return True


def _remove_post_tool_hook() -> bool:
    """Remove any PostToolUse entry whose command carries our marker. Idempotent."""
    if not _CLAUDE_SETTINGS_PATH.exists():
        return False
    settings = _read_config(_CLAUDE_SETTINGS_PATH)
    if not _settings_has_aztea_hook(settings):
        return False
    hooks_root = settings.get("hooks")
    if not isinstance(hooks_root, dict):
        return False
    post_tool = hooks_root.get("PostToolUse")
    if not isinstance(post_tool, list):
        return False
    pruned: list[Any] = []
    for entry in post_tool:
        if not isinstance(entry, dict):
            pruned.append(entry)
            continue
        kept_hooks = [
            h for h in (entry.get("hooks") or [])
            if not (isinstance(h, dict) and _HOOK_MARKER in str(h.get("command") or ""))
        ]
        if not kept_hooks:
            # Drop the whole matcher entry when nothing else lives under it.
            continue
        pruned.append({**entry, "hooks": kept_hooks})
    if pruned:
        hooks_root["PostToolUse"] = pruned
    else:
        hooks_root.pop("PostToolUse", None)
        if not hooks_root:
            settings.pop("hooks", None)
    _write_config(_CLAUDE_SETTINGS_PATH, settings)
    return True


# ── install ────────────────────────────────────────────────────────────────

@app.command()
def install(
    client: str = typer.Option("claude", "--client", help="Editor: claude | cursor."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    with_rule: bool = typer.Option(
        True,
        "--with-rule/--no-with-rule",
        help=(
            "Append the Aztea reflex-routing rule to ~/.claude/CLAUDE.md so the "
            "model calls do_specialist_task(dry_run=true) before responding to "
            "code/config/infra/security/live-data prompts. Claude target only."
        ),
    ),
    with_hook: bool = typer.Option(
        True,
        "--with-hook/--no-with-hook",
        help=(
            "Wire a PostToolUse hook into ~/.claude/settings.json that nudges the "
            "model toward do_specialist_task after Edit/Write/MultiEdit. Claude "
            "target only. Output goes to stderr — does not consume the model's "
            "tool budget."
        ),
    ),
    json_mode: bool = JsonOpt,
) -> None:
    """Register the Aztea MCP server in the chosen editor."""
    try:
        target = _resolve_target(client)
        cfg = load_config() or {}
        key = (api_key or cfg.get("api_key") or "").strip()
        url = (base_url or cfg.get("base_url") or "https://aztea.ai").rstrip("/")
        if not key:
            from .output import error
            error(
                "No API key configured.",
                hint="Run `aztea login` first, then `aztea mcp install`.",
                code="auth.no_key",
            )
            raise typer.Exit(code=1)

        with spinner("Verifying credentials", json_mode=json_mode):
            with build_client(api_key=key, base_url=url) as client_obj:
                client_obj.auth.me()

        # Ask before touching the editor config file.
        if not json_mode and sys.stdout.isatty():
            answer = typer.prompt(
                f"  Register Aztea MCP server in {target.label} ({target.config_path})?",
                default="Y",
            ).strip().lower()
            if answer not in ("y", "yes", ""):
                from .output import warn as _warn
                _warn("Aborted. Run `aztea mcp install` again to register.")
                raise typer.Exit(code=0)

        data = _read_config(target.config_path)
        servers = data.setdefault("mcpServers", {})
        servers["aztea"] = _server_entry(key, url)
        _write_config(target.config_path, data)

        # Reflex rule + PostToolUse hook are Claude-only — Cursor uses its
        # own rules / settings format and we don't write into it from here.
        rule_written = False
        hook_written = False
        if target.name == "Claude Code":
            if with_rule:
                rule_written = _write_reflex_rule()
            if with_hook:
                hook_written = _write_post_tool_hook()

        if json_mode:
            emit(
                {
                    "installed": True,
                    "client": target.name,
                    "path": str(target.config_path),
                    "reflex_rule_written": rule_written,
                    "post_tool_hook_written": hook_written,
                },
                json_mode=True,
            )
            return

        success(
            f"Installed Aztea MCP server in {target.label}",
            detail=str(target.config_path),
        )
        if rule_written:
            info(f"Added reflex rule to {_CLAUDE_MD_PATH}")
        if hook_written:
            info(f"Added PostToolUse hook to {_CLAUDE_SETTINGS_PATH}")
        info(f"Quit and relaunch {target.label} to activate.")
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


# ── doctor ─────────────────────────────────────────────────────────────────

@app.command()
def doctor(
    client: str = typer.Option("claude", "--client", help="Editor: claude | cursor."),
    json_mode: bool = JsonOpt,
) -> None:
    """Verify the MCP installation is healthy.

    Checks: config file present, aztea entry exists, env vars set, API key
    valid against the live server.
    """
    target = _resolve_target(client)
    checks: list[tuple[str, bool, str]] = []

    config_exists = target.config_path.exists()
    checks.append((f"config file at {target.config_path}", config_exists,
                   "" if config_exists else "run `aztea mcp install`"))

    data = _read_config(target.config_path) if config_exists else {}
    entry = (data.get("mcpServers") or {}).get("aztea") if isinstance(data, dict) else None
    has_entry = isinstance(entry, dict)
    checks.append(("aztea server registered", has_entry,
                   "" if has_entry else "run `aztea mcp install`"))

    env = (entry or {}).get("env") if isinstance(entry, dict) else None
    api_key = ((env or {}).get("AZTEA_API_KEY") or "").strip() if isinstance(env, dict) else ""
    base_url = ((env or {}).get("AZTEA_BASE_URL") or "https://aztea.ai") if isinstance(env, dict) else "https://aztea.ai"
    has_key = bool(api_key)
    checks.append(("API key present", has_key,
                   "" if has_key else "re-run `aztea mcp install`"))

    profile_user = ""
    if has_key:
        try:
            with spinner("Verifying server reachability", json_mode=json_mode):
                with build_client(api_key=api_key, base_url=base_url) as client_obj:
                    profile = client_obj.auth.me()
            profile_user = str(profile.get("username") or "")
        except Exception as exc:
            checks.append(("server reachable + key valid", False, str(exc) or "auth failed"))
        else:
            checks.append(("server reachable + key valid", True, ""))

    all_ok = all(passed for _, passed, _ in checks)

    if json_mode:
        emit(
            {
                "client": target.name,
                "config_path": str(target.config_path),
                "ok": all_ok,
                "checks": [{"name": n, "ok": p, "hint": h} for n, p, h in checks],
                "user": profile_user or None,
                "base_url": base_url,
            },
            json_mode=True,
        )
        if not all_ok:
            raise typer.Exit(code=1)
        return

    _render_doctor(
        target.label, str(target.config_path), base_url, profile_user, checks, all_ok,
    )
    if not all_ok:
        raise typer.Exit(code=1)


# ── uninstall ──────────────────────────────────────────────────────────────

@app.command()
def uninstall(
    client: str = typer.Option("claude", "--client", help="Editor: claude | cursor."),
    json_mode: bool = JsonOpt,
) -> None:
    """Remove the Aztea entry from the editor's MCP config."""
    target = _resolve_target(client)
    if not target.config_path.exists():
        if json_mode:
            emit({"removed": False, "reason": "no config file"}, json_mode=True)
            return
        warn(f"No config at {target.config_path}.")
        return

    data = _read_config(target.config_path)
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or "aztea" not in servers:
        if json_mode:
            emit({"removed": False, "reason": "not installed"}, json_mode=True)
            return
        warn("Aztea is not currently registered in this client.")
        return

    del servers["aztea"]
    if not servers:
        data.pop("mcpServers", None)
    _write_config(target.config_path, data)

    # Reverse the install-time CLAUDE.md + hook writes when removing the
    # claude integration. Cursor target leaves nothing to clean up because
    # the rule + hook are never written for it.
    rule_removed = False
    hook_removed = False
    if target.name == "Claude Code":
        rule_removed = _remove_reflex_rule()
        hook_removed = _remove_post_tool_hook()

    if json_mode:
        emit(
            {
                "removed": True,
                "path": str(target.config_path),
                "reflex_rule_removed": rule_removed,
                "post_tool_hook_removed": hook_removed,
            },
            json_mode=True,
        )
        return
    success(f"Removed Aztea from {target.label}", detail=str(target.config_path))
    if rule_removed:
        info(f"Removed reflex rule from {_CLAUDE_MD_PATH}")
    if hook_removed:
        info(f"Removed PostToolUse hook from {_CLAUDE_SETTINGS_PATH}")


# ── serve ──────────────────────────────────────────────────────────────────

@app.command()
def serve(
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
) -> None:
    """Run the stdio MCP server (called by editors, not humans).

    1.6.2 consolidated the MCP server into the ``aztea`` SDK package
    (``aztea.mcp.server``). Pre-1.6.2 this command shelled out to
    ``npx -y aztea-cli mcp`` because the server lived in a separate JS
    implementation on npm — that path drifted from the Python source and
    caused the 1.6.1 co-pilot-mode P0 (broken steer). Now we call the
    Python MCP server directly, in-process. Most users should not run this
    directly; ``aztea mcp install`` wires up the editor to spawn it on
    demand.
    """
    cfg = load_config() or {}
    key = (api_key or cfg.get("api_key") or "").strip()
    url = (base_url or cfg.get("base_url") or "https://aztea.ai").rstrip("/")

    # Set env so the in-process server sees the right key/url even if
    # the caller didn't export them. Mirrors what the old npx wrapper did.
    if key:
        os.environ["AZTEA_API_KEY"] = key
    os.environ["AZTEA_BASE_URL"] = url

    # Lazy-import so a busted MCP module never blocks `aztea --help` /
    # `aztea login` / etc.
    from aztea.mcp.server import main as _serve

    try:
        # Pass an explicit empty argv so the inner argparse doesn't try to
        # re-parse Typer's "mcp serve" tokens still living in sys.argv —
        # would crash with "unrecognized arguments: mcp serve" before any
        # stdio handshake. The Typer layer already extracted api_key /
        # base_url and exported them as env above.
        _serve(argv=[])
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


def _render_doctor(
    label: str,
    config_path: str,
    base_url: str,
    profile_user: str,
    checks: list[tuple[str, bool, str]],
    all_ok: bool,
) -> None:
    """Sectioned checklist with summary panel — used by `aztea mcp doctor`."""
    if not _HAS_RICH:
        banner(f"aztea mcp doctor — {label}")
        for name, passed, hint in checks:
            glyph = CHECK if passed else CROSS
            console.print(f"  {glyph}  {name}" + (f"  ({hint})" if not passed and hint else ""))
        if all_ok:
            kv_table([
                ("client", label), ("config", config_path),
                ("base url", base_url), ("user", profile_user or "—"),
            ])
        else:
            warn("Run `aztea mcp install` to fix.")
        return

    from rich.text import Text
    from rich.panel import Panel
    from rich.table import Table
    from rich.console import Group
    from rich.padding import Padding
    from rich import box

    section("mcp doctor", label)

    rows = Table(show_header=False, show_edge=False, box=None, padding=(0, 1))
    rows.add_column(width=2, no_wrap=True)
    rows.add_column(no_wrap=False)
    rows.add_column(style="muted")
    for name, passed, hint in checks:
        glyph = Text(CHECK, style="success") if passed else Text(CROSS, style="error")
        check_name = Text(name, style="default" if passed else "error")
        hint_text = Text(hint if (not passed and hint) else "", style="muted")
        rows.add_row(glyph, check_name, hint_text)
    console.print(Padding(rows, (1, 0, 1, 1)))

    # Summary card
    if all_ok:
        summary = Table(show_header=False, show_edge=False, box=None, padding=(0, 2))
        summary.add_column(justify="right", style="muted", no_wrap=True)
        summary.add_column(style="default")
        summary.add_row("client", label)
        summary.add_row("config", config_path)
        summary.add_row("base url", base_url)
        summary.add_row("user", profile_user or "—")

        head = Text()
        head.append(f"{BAR} ", style="success")
        head.append("integration healthy", style="success")
        head.append(f"   {DOT}   ", style="border")
        head.append(f"quit and relaunch {label} to pick up changes", style="muted")
        panel = Panel(
            Group(head, Text(""), summary),
            border_style="border_dim",
            box=box.ROUNDED,
            padding=(1, 2),
            title=Text(" ready ", style="bold #0C1F22 on #7EB9B0"),
            title_align="left",
        )
        console.print(panel)
        console.print()
    else:
        head = Text()
        head.append(f"{BAR} ", style="error")
        head.append("integration not healthy", style="error")
        head.append("   run ", style="muted")
        head.append("aztea mcp install", style="code")
        head.append(" to fix", style="muted")
        panel = Panel(
            head, border_style="error", box=box.ROUNDED, padding=(1, 2),
            title=Text(" attention ", style="bold white on #EF4444"),
            title_align="left",
        )
        console.print(panel)
        console.print()
