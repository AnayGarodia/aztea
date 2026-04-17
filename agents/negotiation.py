"""
agent_negotiation.py — Negotiation strategy agent

Input:  {
  "objective": "...",
  "counterparty_profile": "...",
  "constraints": ["..."],
  "context": "..."
}
Output: {
  "opening_position": str,
  "must_haves": [str],
  "tradeables": [str],
  "red_lines": [str],
  "tactics": [{"name": str, "when_to_use": str, "script": str}],
  "fallback_plan": str,
  "risk_flags": [str]
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = (
    "You are an elite negotiations strategist. "
    "Build practical plans with realistic trade-offs. "
    "Return only valid JSON."
)

_USER = """\
Create a negotiation strategy from this context.

Objective:
{objective}

Counterparty profile:
{counterparty_profile}

Constraints:
{constraints}

Additional context:
{context}

Return exactly this JSON shape:
{{
  "opening_position": "best initial proposal in one paragraph",
  "must_haves": ["3-6 non-negotiables to protect value"],
  "tradeables": ["3-6 variables you can flex on for leverage"],
  "red_lines": ["2-5 walk-away conditions"],
  "tactics": [
    {{
      "name": "tactic name",
      "when_to_use": "timing/context",
      "script": "1-3 sentence script to say verbatim"
    }}
  ],
  "fallback_plan": "clear BATNA-style fallback if agreement fails",
  "risk_flags": ["specific risks that could derail the deal"]
}}
"""

_MAX_TEXT_CHARS = 8_000


def run(
    objective: str,
    counterparty_profile: str = "",
    constraints: list[str] | None = None,
    context: str = "",
) -> dict:
    safe_constraints = constraints or []
    user_content = _USER.format(
        objective=objective[:_MAX_TEXT_CHARS],
        counterparty_profile=counterparty_profile[:_MAX_TEXT_CHARS],
        constraints=json.dumps(safe_constraints)[:_MAX_TEXT_CHARS],
        context=context[:_MAX_TEXT_CHARS],
    )
    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", user_content)],
        max_tokens=1200,
        json_mode=True,
    ))
    raw = _strip_fences(resp.text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned non-JSON: {exc}\n\n{raw[:300]}") from exc


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return match.group(1).strip() if match else text
