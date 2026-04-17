"""
agent_negotiation.py — Expert negotiation strategy agent

Input:  {
  "objective": "...",
  "counterparty_profile": "...",
  "constraints": ["..."],
  "context": "...",
  "style": "collaborative|competitive|principled"  # default: principled
}
Output: {
  "power_balance": {"score": int, "assessment": str, "leverage_points": [str]},
  "zopa_analysis": {"exists": bool, "range_description": str, "floor": str, "ceiling": str},
  "opening_position": str,
  "must_haves": [str],
  "tradeables": [{"item": str, "value_to_us": str, "value_to_them": str}],
  "red_lines": [str],
  "batna": {"quality": "strong|moderate|weak", "description": str, "strengthen_by": [str]},
  "tactics": [{"name": str, "when_to_use": str, "script": str, "counter_if_used_against_us": str}],
  "timeline_strategy": str,
  "concession_plan": [{"round": int, "concession": str, "extract_in_return": str}],
  "risk_flags": [{"risk": str, "probability": str, "mitigation": str}],
  "fallback_plan": str
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a principal-level negotiation strategist trained in Harvard's Program on Negotiation, \
Kahneman's behavioral economics research, and real-world M&A, labor, and commercial deal-making. \
You have advised Fortune 500 executives, VCs, and government negotiators on high-stakes deals.

Your strategies are grounded in:
- Fisher & Ury's principled negotiation (separate people from problems, focus on interests not positions)
- ZOPA/BATNA analysis (Zone of Possible Agreement, Best Alternative to Negotiated Agreement)
- Power dynamics: who needs this deal more? What are the walk-away costs for each side?
- Anchoring theory: first offers shape the entire negotiation range
- Behavioral tactics: reciprocity, scarcity framing, social proof, loss aversion activation
- Concession sequencing: decreasing concession size signals approaching limits
- Timing pressure and deadline leverage

Scripts must be verbatim-quality — something a negotiator could say word-for-word.
Counterplay must be specific — how to deflect the same tactic if turned against us.

Return only valid JSON — no markdown, no prose outside the JSON object."""

_USER = """\
Build a comprehensive negotiation strategy. Be specific, tactical, and realistic.

Objective:
{objective}

Counterparty profile:
{counterparty_profile}

Our constraints:
{constraints}

Additional context:
{context}

Negotiation style preference: {style}

Return EXACTLY this JSON:
{{
  "power_balance": {{
    "score": integer 1–10 (1=counterparty dominates, 5=balanced, 10=we dominate),
    "assessment": "frank assessment of who needs this more and why",
    "leverage_points": ["our specific sources of leverage — be concrete"]
  }},
  "zopa_analysis": {{
    "exists": boolean,
    "range_description": "describe the likely zone of possible agreement",
    "floor": "our walk-away threshold",
    "ceiling": "counterparty's likely walk-away threshold"
  }},
  "opening_position": "specific first offer or proposal — include actual numbers/terms if inferable",
  "must_haves": ["3–6 non-negotiables with a one-line rationale for each"],
  "tradeables": [
    {{
      "item": "concession item",
      "value_to_us": "why we can give this up",
      "value_to_them": "why they care about it — asymmetric value is leverage"
    }}
  ],
  "red_lines": ["2–4 deal-breakers with clear reasoning"],
  "batna": {{
    "quality": "strong|moderate|weak",
    "description": "our best outside option if this fails",
    "strengthen_by": ["concrete actions to improve our BATNA before/during negotiation"]
  }},
  "tactics": [
    {{
      "name": "tactic name (e.g. Anchoring, Nibble, Good Cop/Bad Cop, Bracketing)",
      "when_to_use": "exact moment or trigger in the negotiation",
      "script": "2–4 sentences to say verbatim — realistic dialogue",
      "counter_if_used_against_us": "how to deflect this tactic if they use it on us"
    }}
  ],
  "timeline_strategy": "how to use time and deadlines as leverage — be specific",
  "concession_plan": [
    {{
      "round": integer (1, 2, 3...),
      "concession": "what we give up in this round",
      "extract_in_return": "what we demand in exchange — always link concessions to gains"
    }}
  ],
  "risk_flags": [
    {{
      "risk": "specific thing that could derail the deal",
      "probability": "high|medium|low",
      "mitigation": "concrete action to reduce this risk"
    }}
  ],
  "fallback_plan": "detailed BATNA activation plan if deal collapses — next steps and timeline"
}}
"""

_MAX_TEXT_CHARS = 8_000


def run(
    objective: str,
    counterparty_profile: str = "",
    constraints: list[str] | None = None,
    context: str = "",
    style: str = "principled",
) -> dict:
    safe_constraints = constraints or []
    user_content = _USER.format(
        objective=objective[:_MAX_TEXT_CHARS],
        counterparty_profile=counterparty_profile[:_MAX_TEXT_CHARS],
        constraints=json.dumps(safe_constraints)[:_MAX_TEXT_CHARS],
        context=context[:_MAX_TEXT_CHARS],
        style=style[:50],
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
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned non-JSON: {exc}\n\n{raw[:300]}") from exc


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return match.group(1).strip() if match else text
