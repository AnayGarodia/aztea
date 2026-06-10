"""Claude Code deference hooks for the Aztea toolbelt.

# OWNS: the Claude-Code-specific deference adapter — PreToolUse +
#   UserPromptSubmit settings.json wiring, and the Claude-shaped deny-JSON the
#   PreToolUse hook emits.
# NOT OWNS: the pure decision logic (classify, scout, suggestion-builder) now
#   lives in cli/deference_core.py and is re-exported here for back-compat; MCP
#   server registration + the reflex CLAUDE.md rule (cli/mcp.py); the auto-hire
#   decision itself (server core.registry.auto_hire).
# INVARIANTS: hooks FAIL OPEN — a hook never blocks the agent on our own error
#   (bad stdin, network down, parse failure all resolve to exit 0, no output).
#   Settings writes go through the strict-parse guard so we never clobber a
#   settings.json we cannot parse.
# DECISIONS: hook logic lives in hidden `aztea mcp {pretool,prompt}-hook`
#   subcommands (testable, versioned) instead of inline shell. Bash never
#   hard-blocks (too broad a tool); only WebFetch/WebSearch escalate to block.
"""
from __future__ import annotations

import json
from typing import Any, Optional

# Imported lazily-safe: this module is only ever imported from inside mcp.py's
# command bodies (never at mcp import time), so by the time this runs mcp is
# fully initialised. Referencing attributes through `_mcp` (not `from .mcp
# import X`) keeps monkeypatches of mcp._CLAUDE_SETTINGS_PATH honoured in tests.
from . import mcp as _mcp

# The pure decision logic moved to deference_core (2026-06) so OpenClaw/Hermes
# adapters reuse it. Re-exported here so existing callers (cli/mcp.py command
# bodies) and tests that reference `mcp_hooks.<name>` keep working unchanged.
# Patch the cooldown path on deference_core, not here — scout reads it there.
from .deference_core import (  # noqa: F401  (re-export for back-compat)
    Decision,
    build_prompt_suggestion,
    classify_pretool_event,
    classify_pretool_event_for_mode,
    prompt_should_scout,
    scout_specialist,
)

# ── Hook identity: events, matchers, commands, markers ─────────────────────
# The installed command string IS the idempotency marker — it is unique enough
# that no unrelated hook would carry it, and it is what we grep for on remove.
PRETOOL_EVENT = "PreToolUse"
PRETOOL_MATCHER = "WebFetch|WebSearch|Bash"
PRETOOL_MARKER = "aztea mcp pretool-hook"

PROMPT_EVENT = "UserPromptSubmit"
PROMPT_COMMAND = "aztea mcp prompt-hook"
PROMPT_MARKER = PROMPT_COMMAND


def pretool_command(block: bool) -> str:
    """The settings.json command for the PreToolUse hook. Block mode appends
    a flag; the marker substring (PRETOOL_MARKER) is present in both forms."""
    return f"{PRETOOL_MARKER} --mode block" if block else PRETOOL_MARKER


# ── PreToolUse hook runtime (pure: stdin text -> exit/stdout/stderr) ────────

def run_pretool_hook(stdin_text: str, *, mode: str) -> tuple[int, str, str]:
    """Map raw stdin to (exit_code, stdout, stderr). Pure + fail-open so the
    CLI command is a thin shell and the behaviour is unit-testable.

    warn      -> exit 0, stderr nudge (shown in the transcript, never blocks).
    block     -> exit 2 + deny-JSON on stdout + stderr reason (WebFetch/
                 WebSearch only; Bash never escalates in this mode).
    block-all -> as block, but Bash wedge categories deny too (experiments).

    The deny-JSON shape is Claude Code's PreToolUse hook protocol — that is why
    this formatter lives in the Claude adapter, not in deference_core.
    """
    try:
        event = json.loads(stdin_text) if stdin_text.strip() else {}
    except (ValueError, TypeError):
        return 0, "", ""  # malformed event — fail open, say nothing
    decision = classify_pretool_event_for_mode(event, mode)
    if decision is None:
        return 0, "", ""
    # Deny on any block action: under the production modes only web ever
    # blocks, so this is behavior-identical there; under the experimental
    # block-all mode the Bash categories deny too instead of silently
    # downgrading to a warn nobody sees.
    if decision.action == "block":
        deny = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.message,
            }
        })
        return 2, deny, f"[{PRETOOL_MARKER}] {decision.message}"
    return 0, "", f"[{PRETOOL_MARKER}] {decision.message}"


# ── settings.json hook IO (strict-guard write, lenient remove) ─────────────

def settings_has_hook(settings: dict[str, Any], event: str, marker: str) -> bool:
    """True if a hook whose command carries ``marker`` is wired under ``event``."""
    root = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(root, dict):
        return False
    arr = root.get(event)
    if not isinstance(arr, list):
        return False
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict) and marker in str(hook.get("command") or ""):
                return True
    return False


def write_hook(
    event: str, marker: str, command: str, *, matcher: Optional[str] = None
) -> bool:
    """Append a command hook under ``event`` idempotently. Returns True on
    modification, False when already present or when the existing ``hooks``
    structure is the wrong type (refuse to clobber).

    Raises ``mcp._ConfigParseError`` when settings.json exists but is
    unparseable — the caller decides whether to skip (we never overwrite a
    config we cannot read)."""
    settings = _mcp._read_config_or_raise(_mcp._CLAUDE_SETTINGS_PATH)
    if settings_has_hook(settings, event, marker):
        return False
    root = settings.setdefault("hooks", {})
    if not isinstance(root, dict):
        return False
    arr = root.setdefault(event, [])
    if not isinstance(arr, list):
        return False
    entry: dict[str, Any] = {"hooks": [{"type": "command", "command": command}]}
    if matcher is not None:
        entry = {"matcher": matcher, "hooks": entry["hooks"]}
    arr.append(entry)
    _mcp._write_config(_mcp._CLAUDE_SETTINGS_PATH, settings)
    return True


def remove_hook(event: str, marker: str) -> bool:
    """Remove any hook under ``event`` whose command carries ``marker``,
    pruning emptied containers. Idempotent. Uses the lenient read so a corrupt
    file during uninstall is a no-op rather than an error."""
    path = _mcp._CLAUDE_SETTINGS_PATH
    if not path.exists():
        return False
    settings = _mcp._read_config(path)
    if not settings_has_hook(settings, event, marker):
        return False
    root = settings.get("hooks")
    if not isinstance(root, dict):
        return False
    arr = root.get(event)
    if not isinstance(arr, list):
        return False
    pruned: list[Any] = []
    for entry in arr:
        if not isinstance(entry, dict):
            pruned.append(entry)
            continue
        kept = [
            h for h in (entry.get("hooks") or [])
            if not (isinstance(h, dict) and marker in str(h.get("command") or ""))
        ]
        if not kept:
            continue
        pruned.append({**entry, "hooks": kept})
    if pruned:
        root[event] = pruned
    else:
        root.pop(event, None)
        if not root:
            settings.pop("hooks", None)
    _mcp._write_config(path, settings)
    return True


# ── Convenience wrappers used by install / uninstall / doctor ──────────────

def write_pretool_hook(block: bool) -> bool:
    return write_hook(
        PRETOOL_EVENT, PRETOOL_MARKER, pretool_command(block), matcher=PRETOOL_MATCHER
    )


def write_prompt_hook() -> bool:
    # UserPromptSubmit has no tool to match against — omit the matcher.
    return write_hook(PROMPT_EVENT, PROMPT_MARKER, PROMPT_COMMAND)


def remove_pretool_hook() -> bool:
    return remove_hook(PRETOOL_EVENT, PRETOOL_MARKER)


def remove_prompt_hook() -> bool:
    return remove_hook(PROMPT_EVENT, PROMPT_MARKER)


def has_pretool_hook() -> bool:
    return settings_has_hook(_mcp._read_config(_mcp._CLAUDE_SETTINGS_PATH), PRETOOL_EVENT, PRETOOL_MARKER)


def has_prompt_hook() -> bool:
    return settings_has_hook(_mcp._read_config(_mcp._CLAUDE_SETTINGS_PATH), PROMPT_EVENT, PROMPT_MARKER)
