"""
agent_portfolio.py — Expert portfolio planning agent

Input:  {
  "investment_goal": "...",
  "risk_profile": "conservative|moderate|balanced|aggressive|speculative",
  "time_horizon_years": 5,
  "capital_usd": 100000,
  "existing_holdings": "",   # optional: brief description of current holdings
  "constraints": ""          # optional: e.g. no fossil fuels, TFSA limits, etc.
}
Output: {
  "goal_summary": str,
  "risk_assessment": {"profile": str, "max_drawdown_tolerance": str, "volatility_band": str},
  "allocation": [{"bucket": str, "percent": float, "examples": [str], "rationale": str, "risk_level": str}],
  "expected_metrics": {"annual_return_range": str, "volatility_estimate": str, "sharpe_estimate": str},
  "rebalancing_plan": {"frequency": str, "drift_threshold_pct": float, "triggers": [str]},
  "inflation_impact": str,
  "tax_efficiency_notes": [str],
  "watch_metrics": [{"metric": str, "why": str}],
  "phased_deployment": [{"phase": str, "action": str, "timing": str}],
  "red_flags_in_goal": [str],
  "disclaimer": str
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a CFA-level portfolio planning advisor with deep experience in modern portfolio theory, \
factor investing, tax-efficient allocation, and behavioral finance. You have advised both \
high-net-worth individuals and institutional allocators.

Your plans are grounded in:
- Markowitz mean-variance optimization concepts (diversification, efficient frontier)
- Factor exposure: market beta, size, value, momentum, quality, low-volatility
- Risk-adjusted return thinking: Sharpe ratio, Sortino ratio, maximum drawdown
- Tax efficiency: asset location (which accounts hold which assets), tax-loss harvesting
- Behavioral awareness: loss aversion calibration, sequence-of-returns risk for retirees
- Inflation-adjusted real returns — nominal returns are misleading
- Dollar-cost averaging vs. lump-sum deployment tradeoffs

Specific asset examples you suggest are real, investable instruments (ETFs like VTI, BND, VXUS, \
GLD, VTIP; or asset classes that anyone can access). You do NOT recommend individual stocks.

You always include red flags if the stated goal is unrealistic (e.g. expecting 20% annual returns \
safely is impossible; expecting to retire in 3 years on $50k is not viable without major changes).

Return only valid JSON — no markdown, no prose outside the JSON object."""

_USER = """\
Create a comprehensive, expert portfolio plan for this investor profile.

Goal: {investment_goal}
Risk profile: {risk_profile}
Time horizon: {time_horizon_years} years
Capital: ${capital_usd:,.0f}
Existing holdings: {existing_holdings}
Constraints: {constraints}

Return EXACTLY this JSON:
{{
  "goal_summary": "concise restatement of the realistic goal",
  "risk_assessment": {{
    "profile": "conservative|moderate|balanced|aggressive|speculative",
    "max_drawdown_tolerance": "e.g. Can stomach 15% portfolio drop without panic-selling",
    "volatility_band": "estimated annual portfolio volatility range (e.g. 8–12%)"
  }},
  "allocation": [
    {{
      "bucket": "asset class name (e.g. US Equities, Investment Grade Bonds, Real Assets)",
      "percent": float (all must sum to 100.0),
      "examples": ["2–3 real ETFs or fund types (e.g. VTI, VXUS, BND, GLD, VTIP)"],
      "rationale": "why this bucket at this weight for this investor",
      "risk_level": "low|medium|high"
    }}
  ],
  "expected_metrics": {{
    "annual_return_range": "realistic real (inflation-adjusted) return range, e.g. 4–7%%",
    "volatility_estimate": "annual standard deviation estimate, e.g. ~10%%",
    "sharpe_estimate": "rough Sharpe ratio estimate at this allocation"
  }},
  "rebalancing_plan": {{
    "frequency": "how often to review (e.g. quarterly review, annual rebalance)",
    "drift_threshold_pct": float (trigger rebalance when any bucket drifts this many %% from target),
    "triggers": ["event-based rebalance triggers beyond calendar — e.g. market crash >20%%"]
  }},
  "inflation_impact": "plain-English explanation of how inflation erodes this plan and what hedge is built in",
  "tax_efficiency_notes": ["2–4 specific tax efficiency recommendations for this investor's situation"],
  "watch_metrics": [
    {{
      "metric": "specific thing to monitor",
      "why": "why this matters for this portfolio"
    }}
  ],
  "phased_deployment": [
    {{
      "phase": "Phase 1 / Phase 2 / etc.",
      "action": "concrete deployment action",
      "timing": "when to execute relative to start"
    }}
  ],
  "red_flags_in_goal": ["unrealistic expectations or structural problems with the stated goal — be honest"],
  "disclaimer": "Educational content only. Not personalized financial advice. Consult a licensed advisor."
}}
"""


def run(
    investment_goal: str,
    risk_profile: str = "balanced",
    time_horizon_years: int = 5,
    capital_usd: float = 100_000,
    existing_holdings: str = "",
    constraints: str = "",
) -> dict:
    horizon = max(1, min(int(time_horizon_years), 50))
    capital = max(0.0, float(capital_usd))
    user_content = _USER.format(
        investment_goal=investment_goal[:8_000],
        risk_profile=risk_profile[:100],
        time_horizon_years=horizon,
        capital_usd=round(capital, 2),
        existing_holdings=existing_holdings[:1_000] if existing_holdings else "None specified.",
        constraints=constraints[:1_000] if constraints else "None specified.",
    )
    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", user_content)],
        max_tokens=2000,
        json_mode=True,
    ))
    raw = _strip_fences(resp.text)
    try:
        parsed = json.loads(raw)
        parsed.setdefault("disclaimer", "Educational content only. Not personalized financial advice. Consult a licensed advisor.")
        return parsed
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned non-JSON: {exc}\n\n{raw[:300]}") from exc


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return match.group(1).strip() if match else text
