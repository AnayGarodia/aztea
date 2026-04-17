"""
agent_product.py — Expert product strategy agent

Input:  {
  "product_idea": "...",
  "target_users": "...",
  "market_context": "...",
  "horizon_quarters": 2,
  "stage": "idea|pre-seed|seed|series-a|growth"   # default: seed
}
Output: {
  "problem_statement": str,
  "positioning_statement": str,
  "target_segments": [{"segment": str, "size_estimate": str, "pain_intensity": str, "acquisition_channel": str}],
  "competitive_moat": {"moat_type": str, "strength": str, "threats": [str]},
  "jobs_to_be_done": [{"job": str, "frequency": str, "current_solution": str, "our_advantage": str}],
  "roadmap": [{"quarter": str, "theme": str, "bets": [{"feature": str, "rice_score": int, "rationale": str}], "kpis": [str]}],
  "unit_economics": {"cac_estimate": str, "ltv_estimate": str, "ltv_cac_ratio": str, "payback_months": str},
  "experiments": [{"name": str, "hypothesis": str, "metric": str, "minimum_detectable_effect": str, "duration_weeks": int}],
  "go_to_market": {"phase_1": str, "phase_2": str, "phase_3": str},
  "risks": [{"risk": str, "type": str, "mitigation": str}]
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a VP of Product with 12 years of experience at top-tier consumer and B2B SaaS companies. \
You have launched products used by tens of millions of people and advised early-stage startups \
through Series A and beyond.

Your strategy work is grounded in:
- Jobs To Be Done (JTBD) framework — users hire products to make progress; identify the job, not the feature
- RICE scoring for prioritization (Reach × Impact × Confidence / Effort, normalized 1–100)
- Competitive moat analysis: network effects, switching costs, data advantage, brand, scale economies
- Unit economics: CAC/LTV ratio — anything below 3× LTV/CAC at scale is a warning sign
- Hypothesis-driven experimentation: every experiment has a falsifiable hypothesis and a minimum \
  detectable effect to know if it's worth running
- Go-to-market sequencing: nail one channel before expanding; premature scaling kills companies

You are honest about weak spots. If the unit economics don't work, say so. If the moat is thin, say so. \
Founders need reality, not cheerleading.

Return only valid JSON — no markdown, no prose outside the JSON object."""

_USER = """\
Build a rigorous product strategy for this context.

Product idea:
{product_idea}

Target users:
{target_users}

Market context:
{market_context}

Planning horizon: {horizon_quarters} quarters
Company stage: {stage}

Return EXACTLY this JSON:
{{
  "problem_statement": "precise problem being solved — who has it, how often, what it costs them",
  "positioning_statement": "one sentence: For [who] who [need], [product] is [category] that [differentiator]. Unlike [alternative], [product] [key advantage].",
  "target_segments": [
    {{
      "segment": "specific user archetype",
      "size_estimate": "rough TAM/SAM for this segment",
      "pain_intensity": "high|medium|low — how badly do they need this?",
      "acquisition_channel": "most realistic channel to reach them cost-effectively"
    }}
  ],
  "competitive_moat": {{
    "moat_type": "network_effects|switching_costs|data_advantage|brand|cost_structure|regulatory|none",
    "strength": "strong|moderate|thin|none — be honest",
    "threats": ["specific competitors or structural threats to this moat"]
  }},
  "jobs_to_be_done": [
    {{
      "job": "functional job in JTBD format: When [situation], I want to [motivation], so I can [outcome]",
      "frequency": "how often this job arises",
      "current_solution": "what users do today (the workaround or incumbent)",
      "our_advantage": "why we beat the current solution on this job"
    }}
  ],
  "roadmap": [
    {{
      "quarter": "Q1|Q2|etc.",
      "theme": "strategic theme for this quarter",
      "bets": [
        {{
          "feature": "specific feature or initiative",
          "rice_score": integer 1–100,
          "rationale": "R/I/C/E breakdown in one line"
        }}
      ],
      "kpis": ["2–3 measurable leading indicators — not vanity metrics"]
    }}
  ],
  "unit_economics": {{
    "cac_estimate": "estimated customer acquisition cost with channel assumption",
    "ltv_estimate": "estimated lifetime value with assumptions stated",
    "ltv_cac_ratio": "ratio and whether it's healthy at this stage",
    "payback_months": "months to recover CAC"
  }},
  "experiments": [
    {{
      "name": "experiment name",
      "hypothesis": "if we [change], then [metric] will [direction] by [amount], because [reason]",
      "metric": "single primary success metric",
      "minimum_detectable_effect": "smallest improvement worth acting on",
      "duration_weeks": integer
    }}
  ],
  "go_to_market": {{
    "phase_1": "beachhead: specific first 100 customers — who, how, why them",
    "phase_2": "expansion: how to grow to 1000 customers",
    "phase_3": "scale: channel and positioning at mainstream adoption"
  }},
  "risks": [
    {{
      "risk": "specific risk",
      "type": "market|technical|competitive|regulatory|team|timing",
      "mitigation": "concrete action to reduce probability or impact"
    }}
  ]
}}
"""

_MAX_TEXT_CHARS = 8_000


def run(
    product_idea: str,
    target_users: str,
    market_context: str = "",
    horizon_quarters: int = 2,
    stage: str = "seed",
) -> dict:
    horizon = max(1, min(int(horizon_quarters), 8))
    user_content = _USER.format(
        product_idea=product_idea[:_MAX_TEXT_CHARS],
        target_users=target_users[:_MAX_TEXT_CHARS],
        market_context=market_context[:_MAX_TEXT_CHARS],
        horizon_quarters=horizon,
        stage=stage[:50],
    )
    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", user_content)],
        max_tokens=2500,
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
