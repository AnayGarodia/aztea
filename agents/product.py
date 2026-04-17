"""
agent_product.py — Product strategy lab agent

Input:  {
  "product_idea": "...",
  "target_users": "...",
  "market_context": "...",
  "horizon_quarters": 2
}
Output: {
  "positioning_statement": str,
  "user_personas": [str],
  "roadmap": [{"quarter": str, "bets": [str], "kpis": [str]}],
  "experiments": [{"name": str, "hypothesis": str, "metric": str}],
  "risks": [str]
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = (
    "You are a product strategy lead. "
    "Design pragmatic strategy and experiments with measurable outcomes. "
    "Return only valid JSON."
)

_USER = """\
Build a product strategy for:

Product idea:
{product_idea}

Target users:
{target_users}

Market context:
{market_context}

Planning horizon (quarters): {horizon_quarters}

Return exactly this JSON:
{{
  "positioning_statement": "one clear statement",
  "user_personas": ["2-4 concise personas"],
  "roadmap": [
    {{
      "quarter": "Q1/Q2/etc",
      "bets": ["major product bets"],
      "kpis": ["measurable leading indicators"]
    }}
  ],
  "experiments": [
    {{
      "name": "experiment title",
      "hypothesis": "what we believe and why",
      "metric": "single success metric"
    }}
  ],
  "risks": ["key product or GTM risks"]
}}
"""

_MAX_TEXT_CHARS = 8_000


def run(
    product_idea: str,
    target_users: str,
    market_context: str = "",
    horizon_quarters: int = 2,
) -> dict:
    horizon = max(1, min(int(horizon_quarters), 8))
    user_content = _USER.format(
        product_idea=product_idea[:_MAX_TEXT_CHARS],
        target_users=target_users[:_MAX_TEXT_CHARS],
        market_context=market_context[:_MAX_TEXT_CHARS],
        horizon_quarters=horizon,
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
