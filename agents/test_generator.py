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

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a staff engineer specializing in test design and quality assurance. You write tests that \
actually catch bugs — not boilerplate that just achieves coverage numbers.

Given source code, produce a complete, runnable test suite that covers:
1. Happy-path behavior (correct inputs produce correct outputs)
2. Edge cases (empty inputs, boundary values, nulls/undefined)
3. Error conditions (invalid inputs, expected exceptions/errors)
4. Any concurrency or state-mutation hazards visible in the code

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
    code = str(payload.get("code") or "").strip()
    if not code:
        raise ValueError("'code' is required.")

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

    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", prompt)],
        max_tokens=3000,
        json_mode=True,
    ))
    raw = _strip_fences(resp.text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned non-JSON: {e}\n\n{raw[:300]}") from e


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return m.group(1).strip() if m else text
