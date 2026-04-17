"""
agent_scenario.py — Strategic scenario simulation agent

Input:  {
  "decision": "...",
  "assumptions": "...",
  "horizon": "12 months",
  "risk_tolerance": "balanced",
  "key_variables": ["..."]   # optional: specific variables to stress-test
}
Output: {
  "decision": str,
  "horizon": str,
  "scenarios": [{
    "name": str, "archetype": str, "probability": float,
    "narrative": str, "outcome": str,
    "drivers": [str], "trigger_events": [str], "early_signals": [str]
  }],
  "sensitivity_analysis": [{"variable": str, "impact": str, "direction": str}],
  "recommended_plan": {"strategy": str, "next_actions": [str], "risk_mitigations": [str]},
  "pre_mortem": str,
  "monitoring_dashboard": [{"metric": str, "threshold": str, "action_if_breached": str}],
  "confidence": float
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a strategic foresight analyst trained in scenario planning methodologies from \
Shell's pioneering scenario team, the GBN (Global Business Network), and probabilistic \
forecasting as practiced at Metaculus and RAND Corporation.

Your scenarios are:
- Internally consistent — drivers logically produce outcomes
- Distinct — each scenario occupies different probability space
- Actionable — early signals are observable in advance
- Calibrated — probabilities sum to ~1.0 and reflect genuine uncertainty

You use five archetypes: Crash, Downside, Base, Upside, and Moonshot. Each has clear \
preconditions, not just a label.

For sensitivity analysis, you identify which single variable, if wrong, most changes \
the recommended strategy.

For the pre-mortem: imagine it is 2 years from now and the decision failed. What happened?

Return only valid JSON — no markdown, no prose outside the JSON object."""

_USER = """\
Simulate strategic scenarios for this decision context.

Decision: {decision}
Assumptions: {assumptions}
Time horizon: {horizon}
Risk tolerance: {risk_tolerance}
Key variables to stress-test: {key_variables}

Return EXACTLY this JSON:
{{
  "decision": "{decision_short}",
  "horizon": "{horizon}",
  "scenarios": [
    {{
      "name": "scenario name",
      "archetype": "crash|downside|base|upside|moonshot",
      "probability": float (all 5 must sum to ~1.0),
      "narrative": "2-sentence story of how this world unfolds",
      "outcome": "specific, measurable result for the decision-maker",
      "drivers": ["3–5 forces that make this scenario materialize"],
      "trigger_events": ["2–3 observable events that would set this path in motion"],
      "early_signals": ["2–3 leading indicators to watch — with timeframes"]
    }}
  ],
  "sensitivity_analysis": [
    {{
      "variable": "specific assumption or input",
      "impact": "what changes if this variable is wrong by 20%%",
      "direction": "high-impact-if-high|high-impact-if-low|symmetric"
    }}
  ],
  "recommended_plan": {{
    "strategy": "robust strategy that works across base + plausible scenarios",
    "next_actions": ["4–6 concrete actions with timing — not vague platitudes"],
    "risk_mitigations": ["specific hedges against downside/crash scenarios"]
  }},
  "pre_mortem": "Imagine it is {horizon} from now and this decision failed catastrophically. What happened? 3–4 sentences.",
  "monitoring_dashboard": [
    {{
      "metric": "specific KPI or observable signal",
      "threshold": "value or condition that triggers concern",
      "action_if_breached": "concrete response — not 'reassess'",
    }}
  ],
  "confidence": float 0.0–1.0 (model confidence in the scenario structure)
}}

IMPORTANT: Scenarios must be distinct, internally consistent, and actionable. \
Probabilities must sum to approximately 1.0. Include all 5 archetypes (crash through moonshot).
"""

_MAX_TEXT_CHARS = 8_000


def run(
    decision: str,
    assumptions: str = "",
    horizon: str = "12 months",
    risk_tolerance: str = "balanced",
    key_variables: list[str] | None = None,
) -> dict:
    user_content = _USER.format(
        decision=decision[:_MAX_TEXT_CHARS],
        decision_short=decision[:120],
        assumptions=assumptions[:_MAX_TEXT_CHARS],
        horizon=horizon[:200],
        risk_tolerance=risk_tolerance[:100],
        key_variables=json.dumps(key_variables or [])[:500],
    )
    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", user_content)],
        max_tokens=2200,
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
