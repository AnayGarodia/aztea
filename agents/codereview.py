"""
agent_codereview.py — Expert code review agent

Input:  {
  "code": "...",
  "language": "auto",
  "focus": "all",
  "context": ""          # optional: what this code does / its environment
}
Output: {
  "language_detected": str,
  "score": int (1–10),
  "security_critical": bool,
  "complexity_score": int (1–10, 10 = most complex),
  "issues": [{
    "line_hint": str,
    "severity": "critical|high|medium|low|info",
    "category": "security|performance|bug|style|maintainability|correctness",
    "cwe_id": str | null,       # e.g. "CWE-89" for SQL injection
    "owasp_category": str | null,
    "description": str,
    "fix": str
  }],
  "positive_aspects": [str],
  "test_recommendations": [str],
  "summary": str
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a staff-level software engineer and security researcher with 15+ years of experience across \
security audits, performance optimization, and production incident response. Your reviews are \
referenced by teams at FAANG-tier companies because they surface real, exploitable issues — not \
surface-level style comments.

When reviewing code:
- Identify OWASP Top 10 vulnerabilities by name (SQL injection, XSS, IDOR, SSRF, etc.)
- Assign CWE IDs where applicable (CWE-89 for SQLi, CWE-79 for XSS, CWE-22 for path traversal, etc.)
- Detect performance anti-patterns: N+1 queries, unbounded allocations, blocking I/O in hot paths
- Flag correctness bugs: off-by-one, integer overflow, race conditions, exception swallowing
- Note missing error handling at system boundaries (external APIs, file I/O, user input)
- Suggest specific, copy-paste-ready fixes — not vague recommendations
- Be direct: if code has critical flaws, say so

Return only valid JSON — no markdown, no prose outside the JSON object."""

_USER = """\
Review this {language} code. Focus: {focus} (all = security + bugs + performance + maintainability).
Context about this code: {context}

Return a JSON object with EXACTLY these fields:
{{
  "language_detected": "actual language of the snippet",
  "score": integer 1–10 (1=dangerous/broken, 5=acceptable, 8=solid, 10=exemplary),
  "security_critical": boolean (true if any critical/high security issue found),
  "complexity_score": integer 1–10 (cognitive complexity; 10=extremely hard to reason about),
  "issues": [
    {{
      "line_hint": "quoted code snippet or line range context",
      "severity": "critical|high|medium|low|info",
      "category": "security|performance|bug|style|maintainability|correctness",
      "cwe_id": "CWE-XXX or null",
      "owasp_category": "e.g. A03:2021-Injection or null",
      "description": "precise explanation: what is wrong, why it matters, what an attacker/runtime can do",
      "fix": "specific, concrete code change — show the corrected snippet"
    }}
  ],
  "positive_aspects": ["concrete things done well — reference specific patterns"],
  "test_recommendations": ["specific test cases to add: name the scenario and expected behavior"],
  "summary": "2–3 sentence expert verdict including overall risk posture"
}}

Code to review:
```
{code}
```
"""

_MAX_CODE_CHARS = 14_000


def run(code: str, language: str = "auto", focus: str = "all", context: str = "") -> dict:
    """Run LLM-based structured code review and return findings by category.

    Args:
        code: Source code to review (truncated to ``_MAX_CODE_CHARS``).
        language: Language hint; ``"auto"`` lets the LLM detect it.
        focus: Review focus — ``"all"`` | ``"security"`` | ``"performance"``
               | ``"style"`` | ``"bugs"``.
        context: Optional free-text context (e.g. PR description, truncated
                 to 500 chars).

    Returns a dict with ``findings`` (list), ``summary``, ``language``, and
    ``severity_counts``.  Raises ``LLMError`` if no provider is available.
    """
    user_content = _USER.format(
        language=language if language != "auto" else "auto-detect",
        focus=focus,
        context=context[:500] if context else "Not provided.",
        code=code[:_MAX_CODE_CHARS],
    )
    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", user_content)],
        max_tokens=2000,
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
