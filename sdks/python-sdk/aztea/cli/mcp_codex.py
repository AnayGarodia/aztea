"""Codex CLI (TOML) MCP-config helpers, factored out of cli/mcp.py.

# OWNS: reading/writing the Aztea MCP entry in ~/.codex/config.toml via a
#   marker-fenced block.
# NOT OWNS: JSON-client config (cli/mcp.py), hook wiring (cli/mcp_hooks.py).
# DECISIONS: Codex uses TOML. There is no TOML writer in the stdlib (and we
#   support Python 3.10, which has no tomllib reader either), so we manage a
#   fenced block instead of round-tripping the whole file — same approach as
#   the CLAUDE.md reflex rule. We own the bytes between the fences; everything
#   else in the user's config.toml is left untouched.

These names are re-exported from cli/mcp.py for backward compatibility (tests
and callers reference them as ``mcp._codex_*`` / ``mcp._CODEX_BEGIN``).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_CODEX_BEGIN = "# >>> aztea mcp (managed) — edit via `aztea mcp install/uninstall` >>>"
_CODEX_END = "# <<< aztea mcp (managed) <<<"


def _raise_if_invalid_toml(text: str, path: Path) -> None:
    """Refuse to write a config.toml that wouldn't parse. tomllib is 3.11+; on
    3.10 we skip (no stdlib reader) — the block we emit is well-formed by
    construction, so the only unvalidated risk there is pre-existing user edits."""
    try:
        import tomllib
    except ModuleNotFoundError:
        return
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"Refusing to write {path}: the result would not be valid TOML ({exc}). "
            "The existing config likely has a hand-edited or duplicated aztea fence — "
            "fix it and re-run `aztea mcp install`."
        ) from exc


def _toml_basic_string(value: str) -> str:
    """Quote ``value`` as a TOML basic string. TOML basic strings forbid raw
    control characters (a literal newline/tab would split the inline table and
    corrupt the whole config.toml), so escape backslash, quote, AND the control
    chars that can appear in a fat-fingered key/url. API keys are alnum, but
    escape defensively — the value is the user's own, never a remote channel."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _codex_block(api_key: str, base_url: str) -> str:
    """The fenced ``[mcp_servers.aztea]`` block. Env is an inline table so the
    whole entry stays inside the fence."""
    env = (
        f"env = {{ AZTEA_API_KEY = {_toml_basic_string(api_key)}, "
        f"AZTEA_BASE_URL = {_toml_basic_string(base_url)} }}"
    )
    return (
        f"{_CODEX_BEGIN}\n"
        "[mcp_servers.aztea]\n"
        'command = "aztea"\n'
        'args = ["mcp", "serve"]\n'
        f"{env}\n"
        f"{_CODEX_END}\n"
    )


def _codex_has_entry(path: Path) -> bool:
    if not path.exists():
        return False
    return _CODEX_BEGIN in path.read_text(encoding="utf-8")


def _codex_extract_env(path: Path) -> dict[str, str]:
    """Best-effort pull of AZTEA_* env values from the fenced block, for
    `doctor`. Regex (not a TOML parse) so it works on every supported Python."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    start = text.find(_CODEX_BEGIN)
    if start < 0:
        return {}
    end = text.find(_CODEX_END, start)
    block = text[start : end if end >= 0 else len(text)]
    env: dict[str, str] = {}
    for name in ("AZTEA_API_KEY", "AZTEA_BASE_URL"):
        match = re.search(rf'{name}\s*=\s*"((?:[^"\\]|\\.)*)"', block)
        if match:
            env[name] = match.group(1).replace('\\"', '"').replace("\\\\", "\\")
    return env


def _codex_write_entry(path: Path, api_key: str, base_url: str) -> bool:
    """Add or refresh the fenced Codex block. Idempotent: a second call with
    the same values rewrites identical bytes and reports no change."""
    block = _codex_block(api_key, base_url)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if _CODEX_BEGIN in existing:
        start = existing.find(_CODEX_BEGIN)
        end_idx = existing.find(_CODEX_END, start)
        if end_idx < 0:
            # Truncated/corrupted fence — replace from BEGIN to EOF.
            new_text = existing[:start] + block
        else:
            end = end_idx + len(_CODEX_END)
            if end < len(existing) and existing[end] == "\n":
                end += 1
            new_text = existing[:start] + block + existing[end:]
        if new_text == existing:
            return False
    else:
        prefix = existing
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"  # blank line so the block reads as its own section
        new_text = prefix + block
    # Fail loud, don't write broken config: if the result doesn't parse as TOML
    # (e.g. a hand-edited / duplicated fence orphaned surrounding keys), refuse
    # rather than corrupt the user's whole Codex config. tomllib is 3.11+, so
    # on 3.10 we skip validation (the block we emit is well-formed by
    # construction; the risk is only pre-existing user edits).
    _raise_if_invalid_toml(new_text, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    # config.toml carries the API key — keep it owner-only.
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return True


def _codex_remove_entry(path: Path) -> bool:
    """Strip the fenced Codex block. Idempotent."""
    if not path.exists():
        return False
    existing = path.read_text(encoding="utf-8")
    start = existing.find(_CODEX_BEGIN)
    if start < 0:
        return False
    end_idx = existing.find(_CODEX_END, start)
    if end_idx < 0:
        cleaned = existing[:start]
    else:
        end = end_idx + len(_CODEX_END)
        if end < len(existing) and existing[end] == "\n":
            end += 1
        cleaned = existing[:start] + existing[end:]
    cleaned = cleaned.rstrip("\n")
    cleaned = (cleaned + "\n") if cleaned else ""
    path.write_text(cleaned, encoding="utf-8")
    return True
