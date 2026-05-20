"""
regex_tester.py — Test a regex pattern against one or more strings.

Input:
  {
    "pattern": "\\d+",                       # required, Python re syntax
    "test_string": "abc 123 def 456",        # one OR
    "test_strings": ["abc 123", "def 456"],  # many
    "flags": ["IGNORECASE", "MULTILINE"]     # optional list
  }

Output:
  {
    "pattern": str,
    "flags_applied": [str],
    "results": [{
      "test_string": str,
      "matched": bool,
      "matches": [{"match": str, "span": [int, int], "groups": [...]}]
    }],
    "compile_error": str | null
  }

OWNS: regex compilation, multi-string testing, match envelope shaping.
NOT OWNS: regex authoring/repair — that's a chat task, not a specialist call.
INVARIANTS:
  * Compilation errors return a structured envelope, never raise.
  * Timeout-style ReDoS protection: pattern length capped + match wall-clock
    bounded; runaway patterns are rejected rather than allowed to wedge a
    worker.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from agents._contracts import agent_error as _err


_MAX_PATTERN_CHARS = 2_000
_MAX_TEST_STRINGS = 50
_MAX_TEST_STRING_CHARS = 50_000
_MAX_MATCHES_PER_STRING = 200
# L-4 (audit 2026-05-19): per-test-string match budget. Pre-fix,
# catastrophic-backtracking patterns like (a+)+b ran until the wall-clock
# budget killed the worker, surfacing as an empty 502. With a per-string
# budget we kill the runaway BEFORE the wall budget and return a
# structured `regex_tester.match_timeout` envelope the caller can act on.
_PER_STRING_MATCH_BUDGET_SECONDS = 2.0
# Heuristic ReDoS detector: nested unbounded quantifiers like (a+)+,
# (a*)*, (a+)*, etc. are the canonical pathological pattern. We refuse
# them up front so the budget kill above is a safety net, not the first
# line of defense.
_REDOS_NESTED_QUANTIFIER_RE = re.compile(
    r"\([^()]*[+*][^()]*\)[+*]"
)
_SUPPORTED_FLAGS = {
    "IGNORECASE": re.IGNORECASE,
    "I": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "M": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "S": re.DOTALL,
    "UNICODE": re.UNICODE,
    "U": re.UNICODE,
    "ASCII": re.ASCII,
    "A": re.ASCII,
    "VERBOSE": re.VERBOSE,
    "X": re.VERBOSE,
}


def _normalize_flags(raw: Any) -> tuple[int, list[str]]:
    """Pure: turn the caller's ``flags`` list into an OR'd int + canonical names.

    Why: callers pass either short or long names ("I" vs "IGNORECASE");
    centralising the mapping keeps the surface uniform and rejects unknown
    flags loudly at the boundary.
    """
    flags_int = 0
    applied: list[str] = []
    if raw is None:
        return 0, []
    if not isinstance(raw, list):
        raise ValueError("flags must be a list of strings")
    seen: set[str] = set()
    for item in raw:
        name = str(item or "").strip().upper()
        if not name:
            continue
        flag_val = _SUPPORTED_FLAGS.get(name)
        if flag_val is None:
            raise ValueError(
                f"unknown flag {item!r}; supported: "
                + ", ".join(sorted(_SUPPORTED_FLAGS))
            )
        if name in seen:
            continue
        seen.add(name)
        flags_int |= flag_val
        applied.append(name)
    return flags_int, applied


def _collect_test_strings(payload: dict) -> list[str]:
    """Pure: gather ``test_string`` / ``test_strings`` into a single list.

    Why: accept either field so callers can be flexible; cap count and
    per-string length so an over-large input can't blow the latency budget.
    """
    out: list[str] = []
    single = payload.get("test_string")
    if isinstance(single, str):
        out.append(single)
    many = payload.get("test_strings")
    if isinstance(many, list):
        for item in many:
            if isinstance(item, str):
                out.append(item)
    if not out:
        raise ValueError("test_string or test_strings is required")
    if len(out) > _MAX_TEST_STRINGS:
        raise ValueError(
            f"too many test strings ({len(out)}); cap is {_MAX_TEST_STRINGS}"
        )
    for s in out:
        if len(s) > _MAX_TEST_STRING_CHARS:
            raise ValueError(
                f"test string exceeds {_MAX_TEST_STRING_CHARS} chars"
            )
    return out


def _match_envelope(m: re.Match[str]) -> dict[str, Any]:
    """Pure: shape one ``re.Match`` into the agent's match record."""
    groups: list[Any] = []
    for grp in m.groups():
        if grp is None:
            groups.append(None)
        else:
            groups.append(grp[:500])
    return {
        "match": m.group(0)[:500],
        "span": list(m.span()),
        "groups": groups,
    }


class _MatchTimeout(Exception):
    """Raised when a single test-string match exceeds the budget. L-4."""


def _find_matches_bounded(
    compiled: re.Pattern[str], text: str, budget_seconds: float,
) -> list[dict[str, Any]]:
    """Walk matches with a wall-clock budget enforced in a worker thread.

    L-4 (audit 2026-05-19): patterns that compile fine but exhibit
    catastrophic backtracking (e.g. ``(a+)+b`` on ``aaaa...X``) would
    block forever in finditer. Pre-fix the worker's wall-clock budget
    killed the call but the response was an empty 502. Now: this helper
    runs the match in a daemon thread and raises ``_MatchTimeout`` if the
    budget elapses; ``run()`` translates that to a structured envelope.
    """
    result_holder: dict[str, Any] = {"out": [], "err": None}

    def _worker() -> None:
        try:
            out: list[dict[str, Any]] = []
            for m in compiled.finditer(text):
                out.append(_match_envelope(m))
                if len(out) >= _MAX_MATCHES_PER_STRING:
                    break
            result_holder["out"] = out
        except Exception as exc:  # noqa: BLE001 — never propagate inside thread
            result_holder["err"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(budget_seconds)
    if worker.is_alive():
        # The thread is still spinning. We can't kill it safely; it will
        # release on its own once the engine returns. Caller surfaces a
        # structured timeout envelope instead.
        raise _MatchTimeout(
            f"regex match exceeded {budget_seconds:.1f}s per-string budget"
        )
    if result_holder["err"] is not None:
        raise result_holder["err"]
    return result_holder["out"]


def _find_matches(compiled: re.Pattern[str], text: str) -> list[dict[str, Any]]:
    """Backwards-compat shim — delegates to the bounded variant."""
    return _find_matches_bounded(compiled, text, _PER_STRING_MATCH_BUDGET_SECONDS)


def run(payload: dict) -> dict:
    """Test a regex against one or more strings; return per-string matches.

    Why: a real specialist (vs an LLM guessing semantics) — uses Python's
    actual ``re`` engine so the result matches what a caller would get
    locally. Useful for verifying claims about regex behavior and for
    quickly checking complex patterns without spinning up an executor.
    """
    if not isinstance(payload, dict):
        return _err("regex_tester.bad_input",
                    f"payload must be dict, got {type(payload).__name__}")
    pattern = str(payload.get("pattern") or "").strip()
    if not pattern:
        return _err("regex_tester.missing_pattern", "'pattern' is required.")
    if len(pattern) > _MAX_PATTERN_CHARS:
        return _err(
            "regex_tester.pattern_too_long",
            f"pattern exceeds {_MAX_PATTERN_CHARS} chars; refusing to compile.",
            details={"pattern_length": len(pattern)},
        )
    # L-4 (audit 2026-05-19): up-front ReDoS heuristic. Nested unbounded
    # quantifiers like (a+)+, (a*)*, (a+)* are the canonical pathological
    # pattern — refuse them before they can wedge a worker. Wall-budget
    # protection in _find_matches_bounded is the safety net.
    if _REDOS_NESTED_QUANTIFIER_RE.search(pattern):
        return _err(
            "regex_tester.likely_redos",
            (
                "pattern contains nested unbounded quantifiers (e.g. "
                "(a+)+ or (a*)*) — refusing to compile because this "
                "shape is the canonical catastrophic-backtracking ReDoS. "
                "Rewrite as a possessive or atomic alternative."
            ),
            details={"pattern": pattern[:120]},
        )
    try:
        flags_int, flags_applied = _normalize_flags(payload.get("flags"))
    except ValueError as exc:
        return _err("regex_tester.invalid_flags", str(exc))
    try:
        test_strings = _collect_test_strings(payload)
    except ValueError as exc:
        return _err("regex_tester.invalid_test_strings", str(exc))
    try:
        compiled = re.compile(pattern, flags_int)
    except re.error as exc:
        return {
            "pattern": pattern,
            "flags_applied": flags_applied,
            "results": [],
            "compile_error": f"{exc}",
        }
    results: list[dict[str, Any]] = []
    for s in test_strings:
        try:
            matches = _find_matches_bounded(
                compiled, s, _PER_STRING_MATCH_BUDGET_SECONDS,
            )
        except _MatchTimeout as exc:
            return _err(
                "regex_tester.match_timeout",
                str(exc),
                details={
                    "pattern": pattern[:120],
                    "test_string_preview": s[:80],
                    "budget_seconds": _PER_STRING_MATCH_BUDGET_SECONDS,
                    "hint": (
                        "The compiled pattern exhibited catastrophic "
                        "backtracking on this input. Rewrite the regex "
                        "to remove ambiguous overlap (atomic groups / "
                        "possessive quantifiers) or pre-validate inputs."
                    ),
                },
            )
        results.append({
            "test_string": s if len(s) <= 200 else s[:200] + "…",
            "matched": bool(matches),
            "matches": matches,
            "match_count": len(matches),
        })
    return {
        "pattern": pattern,
        "flags_applied": flags_applied,
        "results": results,
        "compile_error": None,
    }
