# OWNS: pure decision logic for `POST /registry/agents/auto-hire` — picks the
#       best agent for a natural-language intent and decides whether to
#       auto-invoke based on confidence + cost + quality + input gates.
# NOT OWNS: the actual call (settlement, refund, signing) — that stays in
#       `registry_call` and is reached via direct in-process function call.
#       This module never touches the wallet, ledger, or HTTP transport.
# INVARIANTS:
#   - `decide()` is pure: given the same (intent, candidates, ctx) it returns
#     the same Decision. It never imports requests or hits HTTP.
#   - The Decision shape is the contract with the route handler. Add new
#     gates by extending Decision.reason values, not by adding side effects.
#   - The auto_invoked=True path requires every gate (confidence, beta,
#     trust, success, price, fields) to pass. Missing one → auto_invoked=False.
#     This is enforced by the test suite — adding a gate without updating
#     tests will fail CI.
# DECISIONS:
#   - We don't reuse the rich verb-rule ranker from scripts/aztea_mcp_server.py.
#     That ranker is tuned for "give me a balanced list to choose from"; auto-
#     invoke needs "is there a CLEAR winner?" — different signal blend. Lean
#     ranker here favors slug/description/tag matches and quality scores.
#   - Confidence is computed from raw signal strength AND dominance margin
#     over the runner-up. A single 90-score candidate fires; two near-tied
#     80-score candidates do not.
#   - Field extraction is intentionally minimal v1: single required string
#     field → fill from intent. Multi-field schemas → return missing_fields
#     and let the LLM re-call with structured input.
# KNOWN DEBT:
#   - No telemetry on which gate fired most often. Add a counter/log when
#     real traffic arrives so we can tune thresholds against data.
#   - No "alternative agent" suggestion when price gate fires (we only
#     return the gated top-1). Could rank within-budget candidates as a
#     fallback list.
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core import feature_flags


# ── Public types ───────────────────────────────────────────────────────────


@dataclass
class CandidateAgent:
    """Subset of an agent dict that the ranker actually reads."""

    agent_id: str
    slug: str
    name: str
    description: str
    tags: list[str]
    category: str
    price_per_call_usd: float
    trust_score: float
    success_rate: float
    stability_tier: str
    input_schema: dict[str, Any]
    raw: dict[str, Any]  # full agent record for downstream public_dict()

    @classmethod
    def from_agent_record(cls, record: dict[str, Any]) -> "CandidateAgent":
        return cls(
            agent_id=str(record.get("agent_id") or ""),
            slug=_derive_slug(record),
            name=str(record.get("name") or ""),
            description=str(record.get("description") or ""),
            tags=[str(t) for t in (record.get("tags") or [])],
            category=str(record.get("category") or ""),
            price_per_call_usd=_safe_float(record.get("price_per_call_usd"), 0.0),
            trust_score=_safe_float(record.get("trust_score"), 0.0),
            success_rate=_safe_float(record.get("success_rate"), 0.0),
            stability_tier=str(record.get("stability_tier") or "").strip().lower(),
            input_schema=dict(record.get("input_schema") or {}),
            raw=record,
        )

    def public_dict(self) -> dict[str, Any]:
        """Compact representation safe to send to the caller."""
        return {
            "agent_id": self.agent_id,
            "slug": self.slug,
            "name": self.name,
            "description": _truncate(self.description, 240),
            "category": self.category,
            "price_per_call_usd": round(self.price_per_call_usd, 4),
            "trust_score": round(self.trust_score, 1),
            "success_rate": round(self.success_rate, 3),
            "stability_tier": self.stability_tier or None,
        }


@dataclass
class Ranked:
    """A scored candidate. Used internally during decide()."""

    candidate: CandidateAgent
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class Decision:
    """The output of decide(). One of two shapes:

    1. auto_invoked=True ⇒ caller proceeds to actually invoke the agent.
       chosen + payload + confidence are populated.
    2. auto_invoked=False ⇒ caller returns the gated response verbatim.
       reason + (candidates | missing_fields | next_step) describe why.
    """

    auto_invoked: bool
    reason: str | None = None  # set when auto_invoked is False
    chosen: CandidateAgent | None = None
    payload: dict[str, Any] | None = None
    confidence: float | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    next_step: str | None = None


# ── Public entrypoint ──────────────────────────────────────────────────────


def decide(
    *,
    intent: str,
    explicit_input: dict[str, Any] | None,
    max_cost_usd: float,
    candidates: list[CandidateAgent],
) -> Decision:
    """Run every auto-invoke gate; return a Decision the caller can act on."""

    if not feature_flags.auto_invoke_enabled():
        return Decision(
            auto_invoked=False,
            reason="disabled",
            next_step="Use aztea_search + aztea_call directly.",
        )

    if not candidates:
        return Decision(
            auto_invoked=False,
            reason="no_match",
            next_step="No agent matched. Try a broader query.",
        )

    intent_text = (intent or "").strip()
    if not intent_text:
        return Decision(
            auto_invoked=False,
            reason="empty_intent",
            next_step="Provide a natural-language intent describing the task.",
        )

    ranked = sorted(
        (_score_candidate(c, intent_text) for c in candidates),
        key=lambda r: r.score,
        reverse=True,
    )
    ranked = [r for r in ranked if r.score > 0]
    if not ranked:
        return Decision(
            auto_invoked=False,
            reason="no_match",
            next_step="No agent matched. Try a broader query.",
        )

    top = ranked[0]
    rest = ranked[1:]

    # ── Gate: confidence ───────────────────────────────────────────────
    confidence = _confidence(top, rest)
    if confidence < feature_flags.auto_invoke_confidence_floor():
        return Decision(
            auto_invoked=False,
            reason="low_confidence",
            confidence=round(confidence, 3),
            candidates=[r.candidate.public_dict() for r in ranked[:3]],
            next_step=(
                "Multiple agents could fit. Call aztea_describe on a candidate, "
                "then aztea_call to run it."
            ),
        )

    # ── Gate: stability tier (no auto-invoke for beta agents) ──────────
    if top.candidate.stability_tier == "beta":
        return Decision(
            auto_invoked=False,
            reason="beta_agent",
            confidence=round(confidence, 3),
            candidates=[top.candidate.public_dict()],
            next_step=(
                f"Top match {top.candidate.slug!r} is in beta. Call aztea_call "
                "explicitly if you want to use it."
            ),
        )

    # ── Gate: trust floor ──────────────────────────────────────────────
    trust_floor = feature_flags.auto_invoke_trust_floor()
    if top.candidate.trust_score < trust_floor:
        return Decision(
            auto_invoked=False,
            reason="low_trust",
            confidence=round(confidence, 3),
            candidates=[top.candidate.public_dict()],
            next_step=(
                f"Top match has trust score {top.candidate.trust_score:.0f}, "
                f"below the auto-invoke floor of {trust_floor:.0f}."
            ),
        )

    # ── Gate: success-rate floor ───────────────────────────────────────
    success_floor = feature_flags.auto_invoke_success_floor()
    # Treat agents with no completed calls (success_rate==0) as eligible —
    # otherwise brand-new agents can never auto-invoke. Only block agents
    # that have a track record AND fall below the floor.
    has_history = top.candidate.raw.get("call_count", 0) >= 5
    if has_history and top.candidate.success_rate < success_floor:
        return Decision(
            auto_invoked=False,
            reason="low_success_rate",
            confidence=round(confidence, 3),
            candidates=[top.candidate.public_dict()],
            next_step=(
                f"Top match has {top.candidate.success_rate:.0%} success rate, "
                f"below the auto-invoke floor of {success_floor:.0%}."
            ),
        )

    # ── Gate: price ────────────────────────────────────────────────────
    price = top.candidate.price_per_call_usd
    server_cap = feature_flags.auto_invoke_server_cap_usd()
    effective_cap = min(max_cost_usd, server_cap)
    if price > effective_cap:
        return Decision(
            auto_invoked=False,
            reason="price_exceeds_max",
            confidence=round(confidence, 3),
            candidates=[top.candidate.public_dict()],
            next_step=(
                f"Top match {top.candidate.slug!r} costs ${price:.2f}. Raise "
                f"max_cost_usd to at least ${price:.2f}, or call aztea_call "
                "explicitly."
            ),
        )

    # ── Gate: required input fields ────────────────────────────────────
    payload, missing = _resolve_payload(top.candidate, intent_text, explicit_input)
    if missing:
        return Decision(
            auto_invoked=False,
            reason="missing_fields",
            confidence=round(confidence, 3),
            candidates=[top.candidate.public_dict()],
            missing_fields=missing,
            next_step=(
                f"Top match {top.candidate.slug!r} needs structured input. "
                f"Re-call with input={{...}} including: {', '.join(missing)}."
            ),
        )

    # All gates passed — caller proceeds to invoke.
    return Decision(
        auto_invoked=True,
        chosen=top.candidate,
        payload=payload,
        confidence=round(confidence, 3),
    )


# ── Ranking ────────────────────────────────────────────────────────────────


def _score_candidate(c: CandidateAgent, intent: str) -> Ranked:
    """Lean confidence-oriented scorer.

    Signals (additive):
      - exact slug match in intent          +50
      - slug substring in intent            +25
      - name match (any token)              +12
      - description-token overlap           +3 per token (cap 24)
      - tag/category match                  +6 per match (cap 18)
      - quality (success * 10 + trust/20)   up to ~14
      - codex_recommended flag              +5
    """
    intent_lower = intent.lower()
    tokens = set(_tokenize(intent_lower))
    if not tokens:
        return Ranked(candidate=c, score=0.0)

    score = 0.0
    reasons: list[str] = []

    slug = c.slug.lower()
    if slug and slug in intent_lower:
        score += 50
        reasons.append(f"slug match: {slug}")
    elif slug and any(part in tokens for part in slug.split("_")):
        score += 25
        reasons.append("slug-fragment match")

    name_tokens = set(_tokenize(c.name.lower()))
    name_overlap = tokens & name_tokens
    if name_overlap:
        score += 12
        reasons.append(f"name match: {','.join(sorted(name_overlap))[:60]}")

    desc_tokens = set(_tokenize(c.description.lower()))
    desc_overlap = tokens & desc_tokens
    if desc_overlap:
        # cap so a long description doesn't crowd out short matches
        score += min(24, len(desc_overlap) * 3)
        reasons.append(f"desc match: {len(desc_overlap)} tokens")

    tag_tokens = {t.lower() for t in c.tags}
    tag_overlap = tokens & tag_tokens
    if tag_overlap:
        score += min(18, len(tag_overlap) * 6)
        reasons.append(f"tag match: {','.join(sorted(tag_overlap))}")

    if c.category and c.category.lower() in tokens:
        score += 6
        reasons.append(f"category match: {c.category}")

    # Quality signal — small boost only when an agent has a real track record.
    if c.raw.get("call_count", 0) >= 5:
        score += min(10, c.success_rate * 10)
        score += min(5, c.trust_score / 20.0)

    if c.raw.get("codex_recommended"):
        score += 5
        reasons.append("recommended")

    return Ranked(candidate=c, score=round(score, 3), reasons=reasons)


def _confidence(top: Ranked, rest: list[Ranked]) -> float:
    """Combine raw signal strength with dominance over the runner-up.

    - raw    = min(1.0, top.score / 100)
    - margin = top.score / max(runner_up.score, 1)   in [1.0, ∞)
    - margin_w = min(1.0, (margin - 1.0))             1× → 0, 2× → 1.0
    - confidence = 0.5 * raw + 0.5 * margin_w

    A single 90-score result with no rivals scores ~0.95. Two near-tied
    80-score results score ~0.40. The 0.55 default floor lets confident
    singletons through and gates ambiguous ties.
    """
    if top.score <= 0:
        return 0.0
    raw = min(1.0, top.score / 100.0)
    if not rest or rest[0].score <= 0:
        return raw
    margin = top.score / max(rest[0].score, 1.0)
    margin_w = min(1.0, max(0.0, margin - 1.0))
    return 0.5 * raw + 0.5 * margin_w


# ── Field extraction ───────────────────────────────────────────────────────


def _resolve_payload(
    agent: CandidateAgent,
    intent: str,
    explicit_input: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Build the payload to pass to the agent, or list missing required fields.

    Strategy:
      1. If `explicit_input` is provided, use it as-is. Validate that every
         required field is present; return missing fields if not.
      2. Otherwise, if the agent has exactly one required field of type
         "string", set it to the intent text.
      3. Otherwise, return missing_fields = required fields and let the LLM
         re-call with structured `input`.
    """
    schema = agent.input_schema if isinstance(agent.input_schema, dict) else {}
    required = list(schema.get("required") or [])
    properties = dict(schema.get("properties") or {})

    if explicit_input is not None:
        missing = [f for f in required if f not in explicit_input]
        return explicit_input, missing

    # Empty schema or no required fields: pass intent verbatim.
    if not required:
        return {"intent": intent}, []

    if len(required) == 1:
        field_name = required[0]
        field_spec = properties.get(field_name) or {}
        field_type = str(field_spec.get("type") or "").lower()
        if field_type in {"string", ""}:
            return {field_name: intent}, []

    return {}, required


# ── Helpers ────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text or "") if len(t) > 2]


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _truncate(text: str, n: int) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _derive_slug(record: dict[str, Any]) -> str:
    """Mirror the slug derivation used by the MCP manifest builder."""
    slug = str(record.get("slug") or "").strip()
    if slug:
        return slug
    name = str(record.get("name") or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "agent"
