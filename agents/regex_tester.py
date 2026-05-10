"""
Regex tester agent — executes Python regex patterns against test strings.

Inputs:
  pattern (str)           — single regex pattern (mutually exclusive with `patterns`)
  patterns (list[str])    — batch of patterns
  test_string (str)       — single string to test (mutually exclusive with `strings`)
  strings (list[str])     — batch of strings to test each pattern against
  flags (list[str])       — optional flag names: IGNORECASE, MULTILINE, DOTALL, VERBOSE, ASCII

Outputs:
  patterns_tested (int)
  strings_tested (int)
  total_matches (int)
  results (list)          — one entry per (pattern x string) pair
  flags_applied (list)

External deps: none — uses stdlib `re` only.
Runtime requirements: none beyond Python 3.10+.
"""

# OWNS: executing Python regex patterns against test strings and returning structured match results
# NOT OWNS: regex syntax documentation, LLM-based pattern generation
# INVARIANTS: never eval() or exec() user input; regex execution is sandboxed by Python's re module
# DECISIONS: findall semantics (all matches) rather than search (first match only) — more useful for validation

import re

MAX_PATTERNS = 10
MAX_STRINGS = 20
MAX_STRING_LEN = 10_000
MAX_MATCHES_PER_RESULT = 100
STRING_TRUNCATE_LEN = 200

_FLAG_MAP: dict[str, re.RegexFlag] = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "VERBOSE": re.VERBOSE,
    "ASCII": re.ASCII,
}


def _error(code: str, message: str) -> dict:
    """Return a top-level structured error envelope."""
    return {"error": {"code": code, "message": message}}


def _parse_flags(flag_names: list[str]) -> tuple[re.RegexFlag, list[str]] | dict:
    """
    Combine flag names into a single re.RegexFlag value.

    Returns (combined_flag, applied_names) or an error envelope dict
    if an unknown flag name is encountered.
    """
    combined = re.RegexFlag(0)
    applied: list[str] = []
    for name in flag_names:
        upper = name.upper()
        if upper not in _FLAG_MAP:
            return _error(
                "regex_tester.unknown_flag",
                f"Unknown flag '{name}'. Allowed: {', '.join(_FLAG_MAP)}",
            )
        combined |= _FLAG_MAP[upper]
        applied.append(upper)
    return combined, applied


def _validate_inputs(payload: dict) -> dict | None:
    """
    Validate payload structure and size limits.

    Returns an error envelope dict on failure, None on success.
    """
    has_pattern = "pattern" in payload
    has_patterns = "patterns" in payload
    has_string = "test_string" in payload
    has_strings = "strings" in payload

    if has_pattern and has_patterns:
        return _error(
            "regex_tester.ambiguous_input",
            "Provide either 'pattern' or 'patterns', not both.",
        )
    if not has_pattern and not has_patterns:
        return _error("regex_tester.missing_pattern", "Provide 'pattern' or 'patterns'.")
    if not has_string and not has_strings:
        return _error("regex_tester.missing_string", "Provide 'test_string' or 'strings'.")

    patterns = [payload["pattern"]] if has_pattern else payload["patterns"]
    strings = [payload["test_string"]] if has_string else payload["strings"]

    if len(patterns) > MAX_PATTERNS:
        return _error(
            "regex_tester.input_too_large",
            f"Too many patterns: {len(patterns)} (max {MAX_PATTERNS}).",
        )
    if len(strings) > MAX_STRINGS:
        return _error(
            "regex_tester.input_too_large",
            f"Too many strings: {len(strings)} (max {MAX_STRINGS}).",
        )
    oversized = [i for i, s in enumerate(strings) if len(s) > MAX_STRING_LEN]
    if oversized:
        return _error(
            "regex_tester.input_too_large",
            f"String(s) at index {oversized} exceed max length {MAX_STRING_LEN}.",
        )
    return None


def _match_info(m: re.Match) -> dict:
    """Extract position, full match text, and group info from a single match object."""
    return {
        "full_match": m.group(0),
        "start": m.start(),
        "end": m.end(),
        "groups": list(m.groups()),
        "named_groups": m.groupdict(),
    }


def _run_pattern_against_string(
    pattern: str, string: str, compiled_flag: re.RegexFlag
) -> dict:
    """
    Execute one (pattern, string) pair and return a result entry.

    Catches re.error per-pattern so a bad pattern does not abort the batch.
    """
    display_string = string[:STRING_TRUNCATE_LEN] if len(string) > STRING_TRUNCATE_LEN else string
    try:
        compiled = re.compile(pattern, compiled_flag)
    except re.error as exc:
        return {
            "pattern": pattern,
            "string": display_string,
            "match_count": 0,
            "matches": [],
            "error": {"code": "regex_tester.invalid_pattern", "message": str(exc)},
            "truncated": False,
        }

    all_matches = list(compiled.finditer(string))
    truncated = len(all_matches) > MAX_MATCHES_PER_RESULT
    capped = all_matches[:MAX_MATCHES_PER_RESULT]

    return {
        "pattern": pattern,
        "string": display_string,
        "match_count": len(all_matches),
        "matches": [_match_info(m) for m in capped],
        "error": None,
        "truncated": truncated,
    }


def _build_results(
    patterns: list[str], strings: list[str], compiled_flag: re.RegexFlag
) -> list[dict]:
    """
    Produce one result entry per (pattern x string) cross-product.

    Pure: no I/O, no side effects.
    """
    return [
        _run_pattern_against_string(pattern, string, compiled_flag)
        for pattern in patterns
        for string in strings
    ]


def run(payload: dict) -> dict:
    """
    Entry point called by the Aztea job runner.

    Validates inputs, compiles flags, executes all (pattern x string) pairs,
    and returns a structured summary. Returns an error envelope on bad input
    rather than raising, so the caller always gets JSON back.
    """
    validation_error = _validate_inputs(payload)
    if validation_error is not None:
        return validation_error

    flag_names: list[str] = payload.get("flags", [])
    flag_result = _parse_flags(flag_names)
    if isinstance(flag_result, dict):
        return flag_result
    compiled_flag, applied_names = flag_result

    patterns = [payload["pattern"]] if "pattern" in payload else payload["patterns"]
    strings = [payload["test_string"]] if "test_string" in payload else payload["strings"]

    results = _build_results(patterns, strings, compiled_flag)
    total_matches = sum(r["match_count"] for r in results)

    # Refund-on-total-failure: if every (pattern x string) result errored AND
    # the caller submitted exactly one pattern, the call delivered no value
    # and should refund — same contract as JWT debugger / dependency_auditor.
    # Per-result errors stay informational when the caller batched multiple
    # patterns (one bad one shouldn't void a useful batch).
    all_errored = bool(results) and all(r.get("error") for r in results)
    if all_errored and len(patterns) == 1:
        first_error = results[0]["error"] or {}
        return {
            "error": {
                "code": first_error.get("code", "regex_tester.invalid_pattern"),
                "message": first_error.get(
                    "message", "Pattern could not be compiled."
                ),
                "details": {
                    "pattern": patterns[0],
                    "patterns_tested": len(patterns),
                    "strings_tested": len(strings),
                },
            }
        }

    return {
        "patterns_tested": len(patterns),
        "strings_tested": len(strings),
        "total_matches": total_matches,
        "results": results,
        "flags_applied": applied_names,
    }
