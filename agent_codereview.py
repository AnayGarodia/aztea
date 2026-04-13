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

import groq as _groq
from groq import Groq

_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama3-70b-8192",
    "mixtral-8x7b-32768",
    "llama-3.1-8b-instant",
]

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
    """
    Review the provided code snippet. Returns a structured review dict.
    Falls back through _MODELS on RateLimitError.
    """
    client = Groq()
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _USER.format(
                language=language if language != "auto" else "auto-detect",
                focus=focus,
                code=code[:_MAX_CODE_CHARS],
            ),
        },
    ]
    last_err = None
    for model in _MODELS:
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=1500, messages=messages
            )
        except _groq.RateLimitError as e:
            last_err = e
            continue
        raw = _strip_fences(resp.choices[0].message.content.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Model {model} returned non-JSON: {e}\n\n{raw[:300]}"
            ) from e
    raise last_err


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return m.group(1).strip() if m else text
