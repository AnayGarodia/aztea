"""Hermes (Nous Research) MCP-config helpers, factored out of cli/mcp.py.

# OWNS: reading/writing the Aztea MCP entry in ~/.hermes/config.yaml under the
#   top-level `mcp_servers` map.
# NOT OWNS: JSON-client config + the install/doctor dispatch (cli/mcp.py); the
#   Codex TOML path (cli/mcp_codex.py); hook wiring (cli/mcp_hooks.py).
# DECISIONS: Hermes config is YAML with a NESTED `mcp_servers.<name>` map, so —
#   unlike the Codex fenced-block approach — we cannot append a managed text
#   block without risking a duplicate top-level `mcp_servers` key (invalid
#   YAML). We parse → merge our single `aztea` entry → dump.
# KNOWN DEBT: PyYAML does not preserve comments, so a full rewrite drops any
#   comments in the user's config.yaml. The writer warns the caller (mcp.py
#   surfaces it). A comment-preserving writer (ruamel) is a future upgrade; not
#   worth a new hard dependency for a v1 registration path.

Re-exported from cli/mcp.py so callers reference them as ``mcp._hermes_*``.
PyYAML is imported lazily so the SDK doesn't hard-require it just for Hermes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

_MCP_KEY = "mcp_servers"  # Hermes' top-level MCP map key in config.yaml


class HermesYamlUnavailable(RuntimeError):
    """Raised when PyYAML is not installed — Hermes registration needs it."""


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse config.yaml into a dict ({} when missing/empty). Raises on
    unparseable YAML so we never clobber a config we can't read (mirrors the
    JSON strict-parse guard)."""
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise HermesYamlUnavailable(
            "Registering with Hermes needs PyYAML. Install it with "
            "`pip install pyyaml` and re-run `aztea mcp install --client hermes`."
        ) from exc
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # PyYAML raises YAMLError (NOT ValueError) on malformed YAML or unknown
        # custom tags. Normalize to ValueError so callers' guards + the install
        # path's clean "could not safely update" refusal actually catch it,
        # instead of an uncaught traceback during `aztea mcp install`.
        raise ValueError(f"{path} is not parseable as safe YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(
            f"Refusing to touch {path}: top-level YAML is not a mapping."
        )
    return loaded


def _entry(api_key: str, base_url: str, client_id: Optional[str]) -> dict[str, Any]:
    env = {"AZTEA_API_KEY": api_key, "AZTEA_BASE_URL": base_url}
    if client_id:
        env["AZTEA_CLIENT_ID"] = client_id
    # No "type": stdio — Hermes infers stdio from command/args (its catalog
    # entries use the same bare command/args/env shape; a url entry would set
    # `url`/`transport` instead).
    return {"command": "aztea", "args": ["mcp", "serve"], "env": env}


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml  # already proven importable by _load_yaml before we get here

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # sort_keys=False preserves the authored top-level ordering as much as
    # safe_dump allows; default_flow_style=False keeps it human-readable block YAML.
    tmp.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)  # config.yaml carries the API key — owner-only.
    tmp.replace(path)


def _hermes_has_entry(path: Path) -> bool:
    try:
        data = _load_yaml(path)
    except (HermesYamlUnavailable, ValueError):
        return False
    servers = data.get(_MCP_KEY)
    return isinstance(servers, dict) and "aztea" in servers


def _hermes_extract_env(path: Path) -> dict[str, str]:
    """Pull AZTEA_* env from the aztea entry, for `doctor`. Best-effort."""
    try:
        data = _load_yaml(path)
    except (HermesYamlUnavailable, ValueError):
        return {}
    servers = data.get(_MCP_KEY)
    entry = servers.get("aztea") if isinstance(servers, dict) else None
    env = entry.get("env") if isinstance(entry, dict) else None
    return {k: str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def _hermes_write_entry(
    path: Path, api_key: str, base_url: str, *, client_id: Optional[str] = None
) -> bool:
    """Add or refresh the aztea entry under ``mcp_servers``. Idempotent: returns
    False when the entry already matches. Raises HermesYamlUnavailable when
    PyYAML is missing, ValueError when the existing YAML can't be parsed safely.

    NOTE: rewrites the whole file via PyYAML, which does not preserve comments
    (see KNOWN DEBT). Surrounding keys/values are preserved."""
    data = _load_yaml(path)
    servers = data.get(_MCP_KEY)
    if not isinstance(servers, dict):
        # Only create when absent/empty/null. Refuse to clobber a non-empty
        # non-dict (e.g. a hand-edited list-form mcp_servers) — silently
        # overwriting it would destroy the user's existing servers.
        if servers:
            raise ValueError(
                f"Refusing to touch {path}: `{_MCP_KEY}` is not a mapping "
                f"(found {type(servers).__name__}); fix it and re-run."
            )
        servers = {}
        data[_MCP_KEY] = servers
    new_entry = _entry(api_key, base_url, client_id)
    if servers.get("aztea") == new_entry:
        return False
    servers["aztea"] = new_entry
    _dump_yaml(path, data)
    return True


def _hermes_remove_entry(path: Path) -> bool:
    """Strip the aztea entry, pruning an emptied ``mcp_servers``. Idempotent."""
    try:
        data = _load_yaml(path)
    except (HermesYamlUnavailable, ValueError):
        return False
    servers = data.get(_MCP_KEY)
    if not isinstance(servers, dict) or "aztea" not in servers:
        return False
    servers.pop("aztea", None)
    if not servers:
        data.pop(_MCP_KEY, None)
    _dump_yaml(path, data)
    return True
