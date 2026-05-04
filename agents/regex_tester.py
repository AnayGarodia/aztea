"""
regex_tester.py — Test a Python regex against samples and return real
matches, groups, and timing. Detects catastrophic-backtracking risk by
running each test sample under a wall-clock budget in a worker process.

Owns:
  - Pattern compilation with stdlib ``re``.
  - Per-sample match execution under a hard timeout (subprocess-isolated
    so a runaway pattern cannot block the agent worker).
  - Reporting groups, named groups, spans, and substitution previews.

Does NOT own:
  - Other engines (PCRE, RE2). Stdlib ``re`` only.
  - Auto-fixing patterns. Reports findings, not rewrites.

Input:
  {
    "pattern": str,                       # required
    "flags": ["IGNORECASE", "MULTILINE", "DOTALL", "VERBOSE"]?,
    "samples": [str],                     # required, 1..50 strings, ≤2KB each
    "operation": "findall" | "match" | "search" | "fullmatch" | "sub",
    "replacement": str,                   # required when operation = "sub"
    "timeout_ms_per_sample": int          # default 200, max 2000
  }

Output:
  {
    "pattern": str,
    "compiled": bool,
    "compile_error": str | None,
    "operation": str,
    "results": [
      {
        "sample_index": int,
        "input_preview": str,
        "matches": [{"start": int, "end": int, "match": str, "groups": [...], "named_groups": {...}}],
        "match_count": int,
        "elapsed_ms": float,
        "timed_out": bool,
        "substitution": str | None
      }
    ],
    "catastrophic_risk": bool,
    "summary": str
  }
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from typing import Any

_MAX_SAMPLES = 50
_MAX_SAMPLE_CHARS = 2048
_MAX_PATTERN_CHARS = 2000
_DEFAULT_TIMEOUT_MS = 200
_MAX_TIMEOUT_MS = 2000

_FLAG_MAP = {
    "IGNORECASE": re.IGNORECASE,
    "I": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "M": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "S": re.DOTALL,
    "VERBOSE": re.VERBOSE,
    "X": re.VERBOSE,
    "ASCII": re.ASCII,
    "A": re.ASCII,
}

_VALID_OPS = {"findall", "match", "search", "fullmatch", "sub"}


def _err(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **details}}


def _resolve_flags(flag_names: list[str]) -> tuple[int, list[str]]:
    flags = 0
    unknown: list[str] = []
    for name in flag_names:
        key = str(name).strip().upper()
        if key in _FLAG_MAP:
            flags |= _FLAG_MAP[key]
        else:
            unknown.append(name)
    return flags, unknown


# Worker script: run as a fresh `python3 -c` subprocess to escape pytest /
# multiprocessing context contamination. Reads job from stdin (JSON), writes
# result to stdout (JSON, single line).
_WORKER_SCRIPT = r"""
import json, re, sys
job = json.loads(sys.stdin.read())
pattern = job["pattern"]
flags = job["flags"]
sample = job["sample"]
operation = job["operation"]
replacement = job["replacement"]
def serialize(m):
    return {
        "start": m.start(),
        "end": m.end(),
        "match": m.group(0),
        "groups": list(m.groups(default=None)),
        "named_groups": dict(m.groupdict(default=None)),
    }
try:
    compiled = re.compile(pattern, flags)
    if operation == "findall":
        out = {"matches": [serialize(m) for m in compiled.finditer(sample)]}
    elif operation == "match":
        m = compiled.match(sample); out = {"matches": [serialize(m)] if m else []}
    elif operation == "search":
        m = compiled.search(sample); out = {"matches": [serialize(m)] if m else []}
    elif operation == "fullmatch":
        m = compiled.fullmatch(sample); out = {"matches": [serialize(m)] if m else []}
    elif operation == "sub":
        new_text, n = compiled.subn(replacement, sample)
        out = {"matches": [], "substitution": new_text, "match_count_override": n}
    else:
        out = {"error": f"unsupported operation {operation!r}"}
except re.error as exc:
    out = {"error": f"re.error: {exc}"}
sys.stdout.write(json.dumps(out))
"""


def _run_one_sample(
    pattern: str,
    flags: int,
    sample: str,
    operation: str,
    replacement: str,
    timeout_ms: int,
) -> dict[str, Any]:
    """Execute one regex operation in an isolated subprocess.

    Subprocess isolation gives us a hard wall-clock timeout that survives
    catastrophic backtracking, and avoids multiprocessing.Pipe / Queue
    flakiness inside pytest's spawn context.
    """
    job = json.dumps(
        {
            "pattern": pattern,
            "flags": int(flags),
            "sample": sample,
            "operation": operation,
            "replacement": replacement,
        }
    )
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _WORKER_SCRIPT],
            input=job,
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000.0,
        )
    except subprocess.TimeoutExpired:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return {
            "timed_out": True,
            "elapsed_ms": round(elapsed_ms, 2),
            "matches": [],
            "error": "timeout",
        }
    except Exception as exc:  # pragma: no cover
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return {
            "timed_out": False,
            "elapsed_ms": round(elapsed_ms, 2),
            "matches": [],
            "error": f"subprocess error: {exc}",
        }

    elapsed_ms = (time.monotonic() - start) * 1000.0

    if proc.returncode != 0 or not proc.stdout.strip():
        return {
            "timed_out": False,
            "elapsed_ms": round(elapsed_ms, 2),
            "matches": [],
            "error": f"worker exit {proc.returncode}: {proc.stderr.strip()[:300]}",
        }
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "timed_out": False,
            "elapsed_ms": round(elapsed_ms, 2),
            "matches": [],
            "error": "worker returned non-JSON output",
        }

    return {"timed_out": False, "elapsed_ms": round(elapsed_ms, 2), **result}


def run(payload: dict) -> dict:
    """Compile and run a regex against samples with backtracking protection."""
    if not isinstance(payload, dict):
        return _err("regex_tester.invalid_payload", "payload must be an object")

    pattern = payload.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _err(
            "regex_tester.missing_pattern",
            "'pattern' is required and must be a non-empty string",
        )
    if len(pattern) > _MAX_PATTERN_CHARS:
        return _err(
            "regex_tester.pattern_too_large",
            f"pattern exceeds {_MAX_PATTERN_CHARS} chars",
        )

    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        return _err(
            "regex_tester.missing_samples",
            "'samples' is required and must be a non-empty list of strings",
        )
    if len(samples) > _MAX_SAMPLES:
        return _err(
            "regex_tester.too_many_samples",
            f"samples may contain at most {_MAX_SAMPLES} entries",
        )
    for i, s in enumerate(samples):
        if not isinstance(s, str):
            return _err("regex_tester.invalid_sample", f"samples[{i}] must be a string")
        if len(s) > _MAX_SAMPLE_CHARS:
            return _err(
                "regex_tester.sample_too_large",
                f"samples[{i}] exceeds {_MAX_SAMPLE_CHARS} chars",
            )

    operation = str(payload.get("operation") or "findall").strip().lower()
    if operation not in _VALID_OPS:
        return _err(
            "regex_tester.invalid_operation",
            f"operation must be one of {sorted(_VALID_OPS)}",
        )

    replacement = ""
    if operation == "sub":
        replacement = payload.get("replacement", "")
        if not isinstance(replacement, str):
            return _err(
                "regex_tester.invalid_replacement",
                "replacement must be a string when operation='sub'",
            )

    flag_input = payload.get("flags") or []
    if not isinstance(flag_input, list):
        return _err("regex_tester.invalid_flags", "flags must be a list of strings")
    flags, unknown_flags = _resolve_flags(flag_input)
    if unknown_flags:
        return _err(
            "regex_tester.unknown_flag",
            f"unknown flag(s): {unknown_flags}; valid flags: {sorted(set(_FLAG_MAP.keys()))}",
        )

    try:
        timeout_ms = int(payload.get("timeout_ms_per_sample", _DEFAULT_TIMEOUT_MS))
    except (TypeError, ValueError):
        return _err(
            "regex_tester.invalid_timeout", "timeout_ms_per_sample must be an integer"
        )
    if timeout_ms <= 0 or timeout_ms > _MAX_TIMEOUT_MS:
        return _err(
            "regex_tester.invalid_timeout",
            f"timeout_ms_per_sample must be in (0, {_MAX_TIMEOUT_MS}]",
        )

    # Try compiling once up front so we report compile errors clearly without
    # spinning a subprocess.
    try:
        re.compile(pattern, flags)
        compiled_ok = True
        compile_error = None
    except re.error as exc:
        return {
            "pattern": pattern,
            "compiled": False,
            "compile_error": f"{exc}",
            "operation": operation,
            "results": [],
            "catastrophic_risk": False,
            "summary": f"Pattern failed to compile: {exc}",
        }

    results: list[dict[str, Any]] = []
    catastrophic = False
    for idx, sample in enumerate(samples):
        outcome = _run_one_sample(
            pattern, flags, sample, operation, replacement, timeout_ms
        )
        if outcome.get("timed_out"):
            catastrophic = True
        match_count = outcome.get("match_count_override")
        if match_count is None:
            match_count = len(outcome.get("matches") or [])
        results.append(
            {
                "sample_index": idx,
                "input_preview": sample if len(sample) <= 120 else sample[:117] + "...",
                "matches": outcome.get("matches", []),
                "match_count": match_count,
                "elapsed_ms": outcome.get("elapsed_ms", 0.0),
                "timed_out": bool(outcome.get("timed_out")),
                "substitution": outcome.get("substitution"),
                "error": outcome.get("error"),
            }
        )

    total_matches = sum(r["match_count"] for r in results)
    if catastrophic:
        summary = "Catastrophic backtracking suspected: at least one sample exceeded the timeout."
    elif total_matches == 0:
        summary = "Pattern compiled but no samples produced matches."
    else:
        summary = f"Pattern matched {total_matches} occurrence(s) across {len(samples)} sample(s)."

    return {
        "pattern": pattern,
        "compiled": compiled_ok,
        "compile_error": compile_error,
        "operation": operation,
        "results": results,
        "catastrophic_risk": catastrophic,
        "summary": summary,
    }
