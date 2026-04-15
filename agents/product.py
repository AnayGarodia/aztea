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
    client = Groq()
    horizon = max(1, min(int(horizon_quarters), 8))
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _USER.format(
                product_idea=product_idea[:_MAX_TEXT_CHARS],
                target_users=target_users[:_MAX_TEXT_CHARS],
                market_context=market_context[:_MAX_TEXT_CHARS],
                horizon_quarters=horizon,
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
