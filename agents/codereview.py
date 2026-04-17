"""
agent_codereview.py — Code review agent

Input:  { "code": "...", "language": "auto", "focus": "all" }
        focus: all | security | performance | bugs | style
Output: { "language_detected": str, "score": int (1-10),
          "issues": [{"line_hint": str, "severity": str, "category": str,
                      "description": str, "fix": str}],
          "positive_aspects": [str], "summary": str }
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = (
    "You are a senior software engineer and security researcher. "
    "You perform thorough, actionable code reviews. "
    "Return only valid JSON — no markdown, no prose outside the JSON."
)

_USER = """\
Review this {language} code. Focus area: {focus} \
(all = bugs + security + performance + style).

Return a JSON object with exactly these fields:
{{
  "language_detected": "string — actual language of the snippet",
  "score": integer from 1 (critical issues) to 10 (near-perfect),
  "issues": [
    {{
      "line_hint": "quoted snippet or line number context",
      "severity": "critical|high|medium|low|info",
      "category": "security|performance|bug|style|maintainability",
      "description": "what is wrong and why it matters",
      "fix": "concrete code change or approach to fix it"
    }}
  ],
  "positive_aspects": ["list of things done well"],
  "summary": "2-3 sentence overall assessment"
}}

Code:
```
{code}
```
"""

_MAX_CODE_CHARS = 12_000


def run(code: str, language: str = "auto", focus: str = "all") -> dict:
    """Review the provided code snippet. Returns a structured review dict."""
    user_content = _USER.format(
        language=language if language != "auto" else "auto-detect",
        focus=focus,
        code=code[:_MAX_CODE_CHARS],
    )
    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", user_content)],
        max_tokens=1500,
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
