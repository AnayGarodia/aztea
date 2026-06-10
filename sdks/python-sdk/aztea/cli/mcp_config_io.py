"""JSON config IO for MCP-client config files, factored out of cli/mcp.py.

# OWNS: lenient + strict reads of an editor/agent JSON config, the owner-only
#   atomic write, the nested server-map walker, and empty-scaffolding pruning.
# NOT OWNS: the install/doctor/uninstall flow + client targets (cli/mcp.py);
#   the TOML (mcp_codex) and YAML (mcp_hermes) writers.
# INVARIANTS: writes are 0600 (configs embed the API key) and atomic (temp +
#   replace). The strict reader refuses to return a non-dict / unparseable
#   config so callers never clobber a file they can't understand.

Pure helpers (path/dict in, value out) with no dependency on the rest of mcp.py,
re-exported from cli/mcp.py so callers/tests keep using ``mcp._read_config`` etc.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


def _read_config(path: Path) -> dict[str, Any]:
    """Lenient read for read-only callers: missing/empty/unparseable all → {}."""
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
    # These editor config files embed the API key in the MCP server env block —
    # keep them owner-only rather than inheriting a 0644 umask.
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class _ConfigParseError(Exception):
    """Raised when a config file exists and is non-empty but won't parse.

    We refuse to write in this case so we never clobber a config we don't
    understand (e.g. a VS Code settings.json carrying JSONC comments).
    """


def _read_config_or_raise(path: Path) -> dict[str, Any]:
    """Like ``_read_config`` but distinguishes 'empty/missing' (safe to
    create, returns {}) from 'exists but unparseable' (raises). Use this on
    the write path; ``_read_config`` stays lenient for read-only callers."""
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _ConfigParseError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise _ConfigParseError("top-level config is not a JSON object")
    return parsed


def _nested_servers(
    data: dict[str, Any], servers_key: tuple[str, ...], *, create: bool
) -> Optional[dict[str, Any]]:
    """Walk ``data`` along ``servers_key`` and return the server map dict.

    With ``create=True`` missing levels are created. With ``create=False``
    a missing or non-dict level returns None (nothing registered)."""
    node: dict[str, Any] = data
    for key in servers_key:
        child = node.get(key)
        if not isinstance(child, dict):
            if not create:
                return None
            child = {}
            node[key] = child
        node = child
    return node


def _prune_empty_path(data: dict[str, Any], servers_key: tuple[str, ...]) -> None:
    """Drop now-empty dicts along ``servers_key`` after a removal, deepest
    first, so uninstall doesn't leave behind empty ``{"mcp": {"servers": {}}}``
    scaffolding."""
    for depth in range(len(servers_key), 0, -1):
        node: dict[str, Any] = data
        ok = True
        for key in servers_key[: depth - 1]:
            nxt = node.get(key)
            if not isinstance(nxt, dict):
                ok = False
                break
            node = nxt
        if not ok:
            continue
        leaf = servers_key[depth - 1]
        child = node.get(leaf)
        if isinstance(child, dict) and not child:
            node.pop(leaf, None)
