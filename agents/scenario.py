"""
agent_scenario.py — Scenario simulation agent

Input:  {
  "decision": "...",
  "assumptions": "...",
  "horizon": "12 months",
  "risk_tolerance": "balanced"
}
Output: {
  "decision": str,
  "horizon": str,
  "risk_tolerance": str,
  "scenarios": [{
    "name": str,
    "probability": float,
    "outcome": str,
    "drivers": [str],
    "early_signals": [str]
  }],
  "recommended_plan": {"strategy": str, "next_actions": [str], "risk_mitigations": [str]},
  "confidence": float
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = (
    "You are a strategic scenario planner. "
    "Create concrete, probabilistic scenarios with practical recommendations. "
    "Return only valid JSON."
)

_USER = """\
Simulate strategic scenarios for this decision:

Decision: {decision}
Assumptions: {assumptions}
Time horizon: {horizon}
Risk tolerance: {risk_tolerance}

Return exactly this JSON:
{{
  "decision": "{decision}",
  "horizon": "{horizon}",
  "risk_tolerance": "{risk_tolerance}",
  "scenarios": [
    {{
      "name": "upside|base|downside (or equivalent)",
      "probability": 0.0,
      "outcome": "concrete expected result",
      "drivers": ["3-5 major drivers"],
      "early_signals": ["signals to monitor early"]
    }}
  ],
  "recommended_plan": {{
    "strategy": "best plan under uncertainty",
    "next_actions": ["4-6 immediate actions"],
    "risk_mitigations": ["specific mitigations for downside risks"]
  }},
  "confidence": 0.0
}}
"""

_MAX_TEXT_CHARS = 8_000


def run(
    decision: str,
    assumptions: str = "",
    horizon: str = "12 months",
    risk_tolerance: str = "balanced",
) -> dict:
    user_content = _USER.format(
        decision=decision[:_MAX_TEXT_CHARS],
        assumptions=assumptions[:_MAX_TEXT_CHARS],
        horizon=horizon[:200],
        risk_tolerance=risk_tolerance[:100],
    )
    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", user_content)],
        max_tokens=1400,
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
