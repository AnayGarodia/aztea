# OWNS: per-run measurement extraction for the builtin-frontier experiment —
#   the harness answer text, tool-call names, and token usage.
# NOT OWNS: invoking the harnesses (runner.py) or scoring (scorer.py).
# INVARIANTS: never fabricate a number — anything unparseable is None,
#   explicitly. No Aztea / DB involvement; this experiment is built-in only.
"""Measurement capture. OpenClaw helpers are reused from the deference
experiment; Hermes one-shot prints only final text on stdout, so tools/usage
come from its newest session JSONL under HERMES_HOME."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

# Reuse the OpenClaw --json extractors from the sibling experiment. Loaded by
# explicit path (not `import capture`) because this module is also named
# `capture`, so a plain import would resolve to itself.
_DEFERENCE_CAPTURE = Path(__file__).resolve().parents[1] / "deference" / "capture.py"
_spec = importlib.util.spec_from_file_location("deference_capture", _DEFERENCE_CAPTURE)
_dc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dc)
openclaw_answer = _dc.openclaw_answer
openclaw_tools = _dc.openclaw_tools
openclaw_usage = _dc.openclaw_usage


def _newest_session_jsonl(hermes_home: Path) -> Path | None:
    """The most recently modified session transcript under HERMES_HOME."""
    candidates = list(hermes_home.glob("sessions/**/*.jsonl")) + list(
        hermes_home.glob("logs/**/*.jsonl")
    )
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def hermes_tools(hermes_home: Path, since_mtime: float) -> list[str] | None:
    """Tool-call names from the newest Hermes session log written after the
    run started. Best-effort; None if no parseable transcript."""
    path = _newest_session_jsonl(hermes_home)
    if path is None or path.stat().st_mtime < since_mtime:
        return None
    tools: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            name = _extract_tool_name(row)
            if name:
                tools.append(name)
    except OSError:
        return None
    return tools or None


def _extract_tool_name(row: Any) -> str | None:
    """Pull a tool-call name from one transcript row across known shapes."""
    if not isinstance(row, dict):
        return None
    for key in ("tool_name", "tool", "name"):
        val = row.get(key)
        if isinstance(val, str) and val:
            return val
    tc = row.get("tool_call") or row.get("function")
    if isinstance(tc, dict):
        name = tc.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def hermes_usage(hermes_home: Path, since_mtime: float) -> dict[str, Any] | None:
    """Token usage from the newest Hermes session log, if present. None when
    unparseable — never a fabricated zero."""
    path = _newest_session_jsonl(hermes_home)
    if path is None or path.stat().st_mtime < since_mtime:
        return None
    found: dict[str, Any] | None = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            usage = row.get("usage") if isinstance(row, dict) else None
            if isinstance(usage, dict) and ("input_tokens" in usage or "input" in usage):
                found = usage
    except OSError:
        return None
    return found


__all__ = [
    "openclaw_answer", "openclaw_tools", "openclaw_usage",
    "hermes_tools", "hermes_usage",
]
