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
    CHECK,
    CROSS,
    banner,
    console,
    emit,
    info,
    kv_table,
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
    """Standard stdio MCP server entry, runnable via npx without a global install."""
    return {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "aztea-cli", "mcp"],
        "env": {
            "AZTEA_API_KEY": api_key,
            "AZTEA_BASE_URL": base_url,
        },
    }


# ── install ────────────────────────────────────────────────────────────────

@app.command()
def install(
    client: str = typer.Option("claude", "--client", help="Editor: claude | cursor."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
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

        if json_mode:
            emit(
                {"installed": True, "client": target.name, "path": str(target.config_path)},
                json_mode=True,
            )
            return

        success(
            f"Installed Aztea MCP server in {target.label}",
            detail=str(target.config_path),
        )
        info("Restart your editor to activate.")
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

    banner(f"aztea mcp doctor — {target.label}")
    for name, passed, hint in checks:
        glyph = CHECK if passed else CROSS
        style = "success" if passed else "error"
        line = f"[{style}]{glyph}[/{style}]  {name}"
        if not passed and hint:
            line += f"  [muted]({hint})[/muted]"
        console.print(line)
    console.print()

    if all_ok:
        kv_table(
            [
                ("client",    target.label),
                ("config",    str(target.config_path)),
                ("base url",  base_url),
                ("user",      profile_user or "—"),
            ],
        )
    else:
        warn("Run `aztea mcp install` to fix.")
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

    if json_mode:
        emit({"removed": True, "path": str(target.config_path)}, json_mode=True)
        return
    success(f"Removed Aztea from {target.label}", detail=str(target.config_path))


# ── serve ──────────────────────────────────────────────────────────────────

@app.command()
def serve(
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
) -> None:
    """Run the stdio MCP server (called by editors, not humans).

    Equivalent to `npx -y aztea-cli mcp` — the npm package owns the server
    entry-point. Most users should not run this directly; `aztea mcp install`
    wires up the editor to spawn it on demand.
    """
    import shutil
    import subprocess

    cfg = load_config() or {}
    key = (api_key or cfg.get("api_key") or "").strip()
    url = (base_url or cfg.get("base_url") or "https://aztea.ai").rstrip("/")

    npx = shutil.which("npx")
    if not npx:
        from .output import error
        error(
            "npx not found.",
            hint="Install Node.js (≥18) so the MCP server can be launched.",
            code="mcp.no_node",
        )
        raise typer.Exit(code=1)

    env = os.environ.copy()
    if key:
        env["AZTEA_API_KEY"] = key
    env["AZTEA_BASE_URL"] = url
    info("Starting Aztea MCP server (Ctrl-C to stop)…")
    try:
        subprocess.run([npx, "-y", "aztea-cli", "mcp"], env=env, check=False)
    except KeyboardInterrupt:
        raise typer.Exit(code=130)
