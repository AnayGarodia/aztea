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

from core.llm import CompletionRequest, Message, run_with_fallback

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
    horizon = max(1, min(int(time_horizon_years), 50))
    capital = max(0.0, float(capital_usd))
    user_content = _USER.format(
        investment_goal=investment_goal[:8_000],
        risk_profile=risk_profile[:100],
        time_horizon_years=horizon,
        capital_usd=round(capital, 2),
    )
    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", user_content)],
        max_tokens=1200,
        json_mode=True,
    ))
    raw = _strip_fences(resp.text)
    try:
        parsed = json.loads(raw)
        parsed.setdefault("disclaimer", "Educational content only. Not financial advice.")
        return parsed
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned non-JSON: {exc}\n\n{raw[:300]}") from exc


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return match.group(1).strip() if match else text
