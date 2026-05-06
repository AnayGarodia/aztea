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
    # High-signal vocabulary curated per-agent for routing. `match_keywords` boost
    # the score when present in the intent; `block_keywords` deduct when present.
    # Both are case-insensitive substring matches against the raw intent text.
    # Defaulted because tests construct CandidateAgent positionally.
    match_keywords: list[str] = field(default_factory=list)
    block_keywords: list[str] = field(default_factory=list)

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
            match_keywords=[
                str(k).lower().strip()
                for k in (record.get("match_keywords") or [])
                if str(k).strip()
            ],
            block_keywords=[
                str(k).lower().strip()
                for k in (record.get("block_keywords") or [])
                if str(k).strip()
            ],
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
    aggressive: bool = False,
) -> Decision:
    """Run every auto-invoke gate; return a Decision the caller can act on.

    aggressive=True lowers the confidence floor to 0.20 (vs the env-tuned
    default 0.30). Trust, price, and stability gates are unchanged. Used by
    callers who want aztea_do to fire on shorter intents.
    """

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
        (_score_candidate(c, intent_text, explicit_input) for c in candidates),
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
    confidence_floor = (
        0.20 if aggressive else feature_flags.auto_invoke_confidence_floor()
    )
    if confidence < confidence_floor:
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


def _score_candidate(
    c: CandidateAgent,
    intent: str,
    explicit_input: dict[str, Any] | None = None,
) -> Ranked:
    """Lean confidence-oriented scorer.

    Signals (additive):
      - exact slug match in intent          +50
      - slug substring in intent            +25
      - name match (any token)              +12
      - description-token overlap           +3 per token (cap 24)
      - tag/category match                  +6 per match (cap 18)
      - quality (success * 10 + trust/20)   up to ~14
      - codex_recommended flag              +5
      - schema-shape match (explicit input  +35
        keys cover ALL required fields)
      - schema-shape partial match          +15
        (≥1 required field present)

    The schema-shape signal exists because intent-string-only routing
    cannot disambiguate "lint this Python" → linter_agent vs
    python_code_executor. When the caller passes input={"code":"..."},
    only agents whose required fields fit that shape get the +35 bump,
    so the decision becomes deterministic.
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

    # Curated routing vocabulary — strongest natural-language signal.
    # match_keywords push the agent toward intents it should serve; block_keywords
    # push it away from intents it should NOT serve (e.g. json_schema_validator
    # should not match "package.json vulnerabilities").
    if c.match_keywords:
        hits = [kw for kw in c.match_keywords if kw and kw in intent_lower]
        if hits:
            score += min(60, len(hits) * 20)
            reasons.append(f"keyword match: {','.join(hits[:3])}")
    if c.block_keywords:
        blocks = [kw for kw in c.block_keywords if kw and kw in intent_lower]
        if blocks:
            score -= min(60, len(blocks) * 30)
            reasons.append(f"blocked by: {','.join(blocks[:3])}")

    # Schema-shape match — the strongest disambiguator when the caller
    # provides an explicit input payload. We don't validate types
    # rigorously; presence of every required key is enough signal.
    # Also checks oneOf/anyOf composite variants so agents like CVE Lookup
    # (no top-level required, only oneOf) receive the bonus when their
    # variant fields are fully provided.
    if isinstance(explicit_input, dict) and c.input_schema:
        required = list((c.input_schema.get("required") or []))
        # Collect composite variants for oneOf/anyOf (same semantics as _resolve_payload).
        schema_for_score = c.input_schema if isinstance(c.input_schema, dict) else {}
        composite_score_variants: list[list[str]] = []
        for _kw in ("oneOf", "anyOf"):
            _variants = schema_for_score.get(_kw)
            if isinstance(_variants, list):
                for _v in _variants:
                    if isinstance(_v, dict):
                        _vreq = list(_v.get("required") or [])
                        if _vreq:
                            composite_score_variants.append(_vreq)
        if required:
            present = [f for f in required if f in explicit_input]
            if len(present) == len(required):
                score += 35
                reasons.append("schema-shape match (all required)")
            elif present:
                score += 15
                reasons.append(f"schema-shape partial ({len(present)}/{len(required)})")
        elif composite_score_variants:
            # No top-level required — check if any composite variant is fully satisfied.
            if any(
                all(f in explicit_input for f in vreq)
                for vreq in composite_score_variants
            ):
                score += 35
                reasons.append("schema-shape match (composite variant)")
            else:
                # Partial credit: find the best-matching variant.
                best = max(
                    composite_score_variants,
                    key=lambda vr: sum(1 for f in vr if f in explicit_input),
                )
                n_present = sum(1 for f in best if f in explicit_input)
                if n_present:
                    score += 15
                    reasons.append(
                        f"schema-shape partial (composite {n_present}/{len(best)})"
                    )

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
    """Build the payload or list missing required fields.

    Handles both top-level ``required`` and composite ``oneOf``/``anyOf``/``allOf``
    variants so agents like CVE lookup (which use oneOf instead of a flat required
    list) are correctly gated.
    """
    schema = agent.input_schema if isinstance(agent.input_schema, dict) else {}
    required = list(schema.get("required") or [])
    properties = dict(schema.get("properties") or {})

    # Collect required fields from composite schema keywords (oneOf/anyOf).
    # oneOf/anyOf: any one variant being satisfied is sufficient.
    # allOf is intentionally excluded — it means ALL sub-schemas must be satisfied
    # simultaneously, not just one, so it requires different handling. No current
    # built-in agent uses allOf for input gating.
    composite_variants: list[list[str]] = []
    for keyword in ("oneOf", "anyOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict):
                    vreq = list(variant.get("required") or [])
                    if vreq:
                        composite_variants.append(vreq)

    if explicit_input is not None:
        missing = [f for f in required if f not in explicit_input]
        if missing:
            return explicit_input, missing
        if composite_variants:
            for variant_required in composite_variants:
                if all(f in explicit_input for f in variant_required):
                    return explicit_input, []
            return explicit_input, [
                f for f in composite_variants[0] if f not in explicit_input
            ]
        return explicit_input, missing

    # No explicit_input. Determine all required fields.
    all_required = required or (composite_variants[0] if composite_variants else [])
    if not all_required:
        return {"intent": intent}, []

    # Single top-level string field: auto-fill from intent.
    if len(all_required) == 1 and not composite_variants:
        field_name = all_required[0]
        field_spec = properties.get(field_name) or {}
        field_type = str(field_spec.get("type") or "").lower()
        if field_type in {"string", ""}:
            return {field_name: intent}, []

    # Composite schema without explicit_input: cannot auto-fill structured fields.
    if composite_variants:
        return {}, composite_variants[0]

    return {}, all_required


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
