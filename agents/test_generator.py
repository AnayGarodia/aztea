"""
test_generator.py — Test suite generation agent

Input:
  {
    "code": "source code to test",
    "language": "python|javascript|typescript|go|java|auto",
    "framework": "pytest|jest|vitest|go_test|junit|auto",
    "style": "unit|integration|both",
    "context": ""  # optional: what the code does, dependencies
  }

Output:
  {
    "language": str,
    "framework": str,
    "test_count": int,
    "coverage_areas": [str],
    "test_code": str,
    "setup_notes": str,
    "summary": str
  }
"""
from __future__ import annotations

from agents._contracts import agent_error, parse_json_payload
from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a staff engineer specializing in test design and quality assurance. You write tests that \
actually catch bugs — not boilerplate that just achieves coverage numbers.

Given source code, produce a complete, runnable test suite that covers:
1. Happy-path behavior (correct inputs produce correct outputs)
2. Edge cases (empty inputs, boundary values, nulls/undefined)
3. Error conditions (invalid inputs, expected exceptions/errors)
4. Any concurrency or state-mutation hazards visible in the code

CRITICAL: Before writing each test, reason through what the code actually does for that input. \
If a value falls outside a validated range and the code raises an exception, write a test that \
uses pytest.raises (or equivalent), NOT a test that asserts a return value. Read the guard \
conditions in the source carefully — a value at the boundary of a raise condition should test \
the raise, not the happy path.

Tests should be self-contained and runnable with standard tooling (no exotic setup required). \
Use descriptive test names that document what behavior is being verified.

Return ONLY valid JSON — no markdown fences, no prose outside the JSON object."""

_USER = """\
Generate a test suite for this {language} code.
Testing framework: {framework}
Test style: {style}
Context: {context}

Source code:
```
{code}
```

Return a JSON object:
{{
  "language": "detected or specified language",
  "framework": "pytest|jest|vitest|go_test|junit|etc",
  "test_count": integer count of test functions/cases,
  "coverage_areas": ["list of behaviors/scenarios covered"],
  "test_code": "complete, runnable test file as a string",
  "setup_notes": "any install or config steps needed to run (empty string if none)",
  "summary": "1–2 sentence description of the coverage approach"
}}"""

_MAX_CODE_CHARS = 12_000


def run(payload: dict) -> dict:
    """Deprecated LLM-only wrapper — sunset 2026-07-26.

    Generates test cases from ``code`` (required). Optional: ``language``, ``framework``,
    ``style`` (unit/integration/both), ``context``.
    """
    code = str(payload.get("code") or "").strip()
    if not code:
        return agent_error("test_generator.missing_code", "code is required.")

    language = str(payload.get("language") or "auto")
    framework = str(payload.get("framework") or "auto")
    style = str(payload.get("style") or "both")
    context = str(payload.get("context") or "Not provided.")[:500]

    prompt = _USER.format(
        language=language,
        framework=framework,
        style=style,
        context=context,
        code=code[:_MAX_CODE_CHARS],
    )

    try:
        resp = run_with_fallback(CompletionRequest(
            model="",
            messages=[Message("system", _SYSTEM), Message("user", prompt)],
            max_tokens=3000,
            json_mode=True,
        ))
        result = parse_json_payload(resp.text)
    except Exception as exc:
        return agent_error("test_generator.tool_unavailable", f"Test generation requires an available LLM provider: {exc}")

    # Syntax-check Python test output so callers don't get silently broken tests
    test_code = result.get("test_code", "")
    detected_lang = str(result.get("language", "")).lower()
    if test_code and detected_lang == "python":
        import ast as _ast
        try:
            _ast.parse(test_code)
        except SyntaxError as e:
            result["syntax_warning"] = (
                f"Generated test code has a syntax error at line {e.lineno}: {e.msg}. "
                "Review and fix before running."
            )
            result.setdefault("billing_units_actual", 1)
            return result

        # Smoke-run: import the test module to catch top-level errors
        # (wrong module names, NameErrors in class bodies, bad decorators, etc.)
        # Test functions are defined but not invoked — we only check module-level validity.
        # ImportError/ModuleNotFoundError for the code under test is expected and ignored.
        try:
            from agents import python_executor as _executor
        except ImportError:
            _executor = None  # type: ignore[assignment]
        if _executor is not None:
            smoke = _executor.run({
                "code": test_code,
                "timeout": 5,
                "explain": False,
            })
            if smoke.get("exit_code", 0) != 0:
                stderr = (smoke.get("stderr") or "").strip()
                # Ignore expected ImportError for the module under test
                if stderr and "ModuleNotFoundError" not in stderr and "ImportError" not in stderr:
                    result.setdefault("syntax_warning", (
                        f"Smoke-run found a top-level error in the generated test code: "
                        f"{stderr[:300]}. Review before running."
                    ))

    result.setdefault("billing_units_actual", 1)
    return result
