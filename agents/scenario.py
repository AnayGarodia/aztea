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
    client = Groq()
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _USER.format(
                decision=decision[:_MAX_TEXT_CHARS],
                assumptions=assumptions[:_MAX_TEXT_CHARS],
                horizon=horizon[:200],
                risk_tolerance=risk_tolerance[:100],
            ),
        },
    ]
    last_err = None
    for model in _MODELS:
        try:
            resp = client.chat.completions.create(
                model=model,
                max_tokens=1400,
                messages=messages,
            )
        except _groq.RateLimitError as exc:
            last_err = exc
            continue
        raw = _strip_fences(resp.choices[0].message.content.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Model {model} returned non-JSON: {exc}\n\n{raw[:300]}") from exc
    raise last_err


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return match.group(1).strip() if match else text
