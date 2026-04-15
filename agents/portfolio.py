"""
agent_portfolio.py — Portfolio planning agent

Input:  {
  "investment_goal": "...",
  "risk_profile": "balanced",
  "time_horizon_years": 5,
  "capital_usd": 100000
}
Output: {
  "goal_summary": str,
  "allocation": [{"bucket": str, "percent": float, "rationale": str}],
  "rebalancing_plan": str,
  "watch_metrics": [str],
  "disclaimer": str
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
    "You are an investment portfolio planning assistant. "
    "Produce balanced, educational allocations with explicit caveats. "
    "Return only valid JSON."
)

_USER = """\
Create an educational portfolio plan using this profile:

Goal: {investment_goal}
Risk profile: {risk_profile}
Time horizon (years): {time_horizon_years}
Capital (USD): {capital_usd}

Return exactly:
{{
  "goal_summary": "short summary",
  "allocation": [
    {{
      "bucket": "asset class/bucket",
      "percent": 0.0,
      "rationale": "why this allocation exists"
    }}
  ],
  "rebalancing_plan": "clear cadence and trigger-based guidance",
  "watch_metrics": ["4-8 metrics to monitor"],
  "disclaimer": "Educational content only. Not financial advice."
}}
"""


def run(
    investment_goal: str,
    risk_profile: str = "balanced",
    time_horizon_years: int = 5,
    capital_usd: float = 100_000,
) -> dict:
    client = Groq()
    horizon = max(1, min(int(time_horizon_years), 50))
    capital = max(0.0, float(capital_usd))
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _USER.format(
                investment_goal=investment_goal[:8_000],
                risk_profile=risk_profile[:100],
                time_horizon_years=horizon,
                capital_usd=round(capital, 2),
            ),
        },
    ]
    last_err = None
    for model in _MODELS:
        try:
            resp = client.chat.completions.create(
                model=model,
                max_tokens=1200,
                messages=messages,
            )
        except _groq.RateLimitError as exc:
            last_err = exc
            continue
        raw = _strip_fences(resp.choices[0].message.content.strip())
        try:
            parsed = json.loads(raw)
            parsed.setdefault("disclaimer", "Educational content only. Not financial advice.")
            return parsed
        except json.JSONDecodeError as exc:
            raise ValueError(f"Model {model} returned non-JSON: {exc}\n\n{raw[:300]}") from exc
    raise last_err


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return match.group(1).strip() if match else text
