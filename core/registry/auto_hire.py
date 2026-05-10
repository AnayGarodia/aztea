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
from typing import Any, Callable

from core import feature_flags

# Probation gates — applied to agents whose review_status is 'probation'
# (set automatically by /registry/register and /onboarding/ingest for non-
# master callers). The penalty is large enough to drop a probation agent
# below any approved peer with even a weak signal, but small enough that an
# explicit slug match in the intent (+50 baseline) still wins.
_PROBATION_RANK_PENALTY = 30.0
_PROBATION_PRICE_CAP_USD = 1.00

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


_AGGRESSIVE_CONFIDENCE_FLOOR = 0.20
_HISTORY_THRESHOLD_CALL_COUNT = 5  # below this, treat as 'too new to penalise'
_TOP_CANDIDATES_PREVIEW = 3


def _check_disabled_or_empty(
    candidates: list[CandidateAgent], intent_text: str,
) -> Decision | None:
    """Pure-ish: gate on the no-side-effect cases (feature flag, empty inputs).

    Why: separating these from the scoring loop lets ``decide`` keep its
    invariant chain — "score → confidence → stability → trust → success →
    price → fields" — readable in one frame.
    """
    if not feature_flags.auto_invoke_enabled():
        return Decision(
            auto_invoked=False,
            reason="disabled",
            next_step="Use search_specialists + call_specialist directly.",
        )
    if not candidates:
        return Decision(
            auto_invoked=False,
            reason="no_match",
            next_step="No agent matched. Try a broader query.",
        )
    if not intent_text:
        return Decision(
            auto_invoked=False,
            reason="empty_intent",
            next_step="Provide a natural-language intent describing the task.",
        )
    return None


def _rank_candidates(
    candidates: list[CandidateAgent], intent_text: str,
    explicit_input: dict[str, Any] | None,
) -> list:
    """Pure: score every candidate and return non-zero matches sorted by descending score."""
    ranked = sorted(
        (_score_candidate(c, intent_text, explicit_input) for c in candidates),
        key=lambda r: r.score,
        reverse=True,
    )
    return [r for r in ranked if r.score > 0]


def _check_confidence_gate(
    top: Any, rest: list, ranked: list, *, aggressive: bool,
) -> tuple[float, Decision | None]:
    """Pure-ish: returns ``(confidence, decision_or_None)``.

    Why: ``aggressive=True`` lowers the floor to 0.20 (vs the env-tuned
    default) so callers who want aztea_do to fire on shorter intents can
    opt in without lowering the floor for everyone else.
    """
    confidence = _confidence(top, rest)
    floor = (
        _AGGRESSIVE_CONFIDENCE_FLOOR
        if aggressive
        else feature_flags.auto_invoke_confidence_floor()
    )
    if confidence < floor:
        return confidence, Decision(
            auto_invoked=False,
            reason="low_confidence",
            confidence=round(confidence, 3),
            candidates=[r.candidate.public_dict() for r in ranked[:_TOP_CANDIDATES_PREVIEW]],
            next_step=(
                "Multiple agents could fit. Call describe_specialist on a candidate, "
                "then call_specialist to run it."
            ),
        )
    return confidence, None


def _check_stability_gate(top: Any, confidence: float) -> Decision | None:
    """Pure: refuse beta agents — direct call_specialist still works."""
    if top.candidate.stability_tier != "beta":
        return None
    return Decision(
        auto_invoked=False,
        reason="beta_agent",
        confidence=round(confidence, 3),
        candidates=[top.candidate.public_dict()],
        next_step=(
            f"Top match {top.candidate.slug!r} is in beta. Call call_specialist "
            "explicitly if you want to use it."
        ),
    )


def _check_trust_gate(top: Any, confidence: float) -> Decision | None:
    """Pure: enforce the env-tunable trust floor against the top candidate's score."""
    trust_floor = feature_flags.auto_invoke_trust_floor()
    if top.candidate.trust_score >= trust_floor:
        return None
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


def _check_success_gate(top: Any, confidence: float) -> Decision | None:
    """Pure: only block agents with a real track record falling below the floor.

    Why: brand-new agents (call_count under the history threshold) are
    not penalised, otherwise they could never auto-invoke.
    """
    has_history = top.candidate.raw.get("call_count", 0) >= _HISTORY_THRESHOLD_CALL_COUNT
    success_floor = feature_flags.auto_invoke_success_floor()
    if not (has_history and top.candidate.success_rate < success_floor):
        return None
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


def _check_quality_gates(top: Any, confidence: float) -> Decision | None:
    """Pure: chain stability + trust + success-rate gates."""
    return (
        _check_stability_gate(top, confidence)
        or _check_trust_gate(top, confidence)
        or _check_success_gate(top, confidence)
    )


def _check_price_gate(top: Any, max_cost_usd: float, confidence: float) -> Decision | None:
    """Pure-ish: per-call price ceiling. Probation listings have a hard cap regardless of caller intent."""
    price = top.candidate.price_per_call_usd
    effective_cap = min(max_cost_usd, feature_flags.auto_invoke_server_cap_usd())
    if str(top.candidate.raw.get("review_status") or "").strip() == "probation":
        effective_cap = min(effective_cap, _PROBATION_PRICE_CAP_USD)
    if price > effective_cap:
        return Decision(
            auto_invoked=False,
            reason="price_exceeds_max",
            confidence=round(confidence, 3),
            candidates=[top.candidate.public_dict()],
            next_step=(
                f"Top match {top.candidate.slug!r} costs ${price:.2f}. Raise "
                f"max_cost_usd to at least ${price:.2f}, or call call_specialist "
                "explicitly."
            ),
        )
    return None


def _missing_fields_decision(top: Any, missing: list[str], confidence: float) -> Decision:
    """Pure: refusal Decision for the 'top candidate is missing required fields' case."""
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


def _no_match_decision() -> Decision:
    """Pure: refusal Decision for the 'no candidate scored above zero' case."""
    return Decision(
        auto_invoked=False,
        reason="no_match",
        next_step="No agent matched. Try a broader query.",
    )


def _attempt_auto_invoke(
    top: Any, ranked: list, intent_text: str,
    explicit_input: dict[str, Any] | None,
    max_cost_usd: float, aggressive: bool,
) -> Decision:
    """Pure-ish: run every gate against the top candidate; produce a Decision."""
    confidence, low_conf = _check_confidence_gate(
        top, ranked[1:], ranked, aggressive=aggressive,
    )
    if low_conf is not None:
        return low_conf
    blocked = (
        _check_quality_gates(top, confidence)
        or _check_price_gate(top, max_cost_usd, confidence)
    )
    if blocked is not None:
        return blocked
    payload, missing = _resolve_payload(top.candidate, intent_text, explicit_input)
    if missing:
        return _missing_fields_decision(top, missing, confidence)
    return Decision(
        auto_invoked=True,
        chosen=top.candidate,
        payload=payload,
        confidence=round(confidence, 3),
    )


def decide(
    *,
    intent: str,
    explicit_input: dict[str, Any] | None,
    max_cost_usd: float,
    candidates: list[CandidateAgent],
    aggressive: bool = False,
) -> Decision:
    """Pure-ish: run every auto-invoke gate; return a ``Decision`` the caller can act on.

    Why: gates run in a fixed order — score → confidence → stability →
    trust → success → price → fields — so callers get a deterministic
    refusal reason rather than a non-deterministic union of failures.
    """
    intent_text = (intent or "").strip()
    early = _check_disabled_or_empty(candidates, intent_text)
    if early is not None:
        return early
    ranked = _rank_candidates(candidates, intent_text, explicit_input)
    if not ranked:
        return _no_match_decision()
    return _attempt_auto_invoke(
        ranked[0], ranked, intent_text, explicit_input, max_cost_usd, aggressive,
    )


# ── Ranking ────────────────────────────────────────────────────────────────


_SLUG_FULL_BONUS = 50
_SLUG_FRAGMENT_BONUS = 25
_NAME_OVERLAP_BONUS = 12
_NAME_OVERLAP_LABEL_CHARS = 60
_DESC_OVERLAP_PER_TOKEN = 3
_DESC_OVERLAP_CAP = 24
_TAG_OVERLAP_PER_TOKEN = 6
_TAG_OVERLAP_CAP = 18
_CATEGORY_BONUS = 6
_QUALITY_TRACK_RECORD_CALLS = 5
_QUALITY_SUCCESS_BONUS_CAP = 10
_QUALITY_TRUST_BONUS_CAP = 5
_TRUST_BONUS_DIVISOR = 20.0
_CODEX_RECOMMENDED_BONUS = 5
_INTENT_INTERLOCK_BONUS = 45
_DEPENDENCY_AUDIT_BONUS = 70
_KEYWORD_MATCH_PER = 20
_KEYWORD_MATCH_CAP = 60
_BLOCK_KEYWORD_PER = 30
_BLOCK_KEYWORD_CAP = 60
_SCHEMA_SHAPE_FULL_BONUS = 35
_SCHEMA_SHAPE_PARTIAL_BONUS = 15
_KEYWORD_PREVIEW_LIMIT = 3

_AUDIT_TOKEN_SET = frozenset({
    "audit", "audits", "auditing", "vulnerability",
    "vulnerabilities", "cve", "cves", "supply",
})
_DEPENDENCY_AGENT_HINTS = ("dependency_auditor", "dependency auditor", "dep-audit")
_EXEC_VERBS = frozenset({"run", "execute", "evaluate", "repl", "interpreter", "compute"})
_PYTHON_TOKENS = frozenset({"python", "py3", "python3"})
_CODEY_HINTS = ("def ", "class ", "import ", "print(", "lambda ")
_LINT_TOKENS = frozenset({"lint", "linter", "ruff", "eslint"})
_BROWSER_TOKENS = frozenset({"browser", "screenshot", "playwright", "homepage"})
_BROWSER_AGENT_HINTS = ("browser", "playwright", "screenshot")
_IMAGE_TOKENS = frozenset({"image", "generate", "generation", "dall", "replicate"})
_IMAGE_AGENT_HINTS = ("image", "generation", "replicate", "gpt-image")
_FINANCIAL_TOKENS = frozenset({"edgar", "10-k", "sec", "revenue"})
_FINANCIAL_AGENT_HINTS = ("edgar", "sec", "financial")


def _score_string_signals(c: CandidateAgent, intent_lower: str, tokens: set[str]) -> tuple[float, list[str]]:
    """Pure: slug / name / description / tag / category bonuses."""
    score = 0.0
    reasons: list[str] = []
    slug = c.slug.lower()
    if slug and slug in intent_lower:
        score += _SLUG_FULL_BONUS
        reasons.append(f"slug match: {slug}")
    elif slug and any(part in tokens for part in slug.split("_")):
        score += _SLUG_FRAGMENT_BONUS
        reasons.append("slug-fragment match")
    name_overlap = tokens & set(_tokenize(c.name.lower()))
    if name_overlap:
        score += _NAME_OVERLAP_BONUS
        reasons.append(f"name match: {','.join(sorted(name_overlap))[:_NAME_OVERLAP_LABEL_CHARS]}")
    desc_overlap = tokens & set(_tokenize(c.description.lower()))
    if desc_overlap:
        score += min(_DESC_OVERLAP_CAP, len(desc_overlap) * _DESC_OVERLAP_PER_TOKEN)
        reasons.append(f"desc match: {len(desc_overlap)} tokens")
    tag_overlap = tokens & {t.lower() for t in c.tags}
    if tag_overlap:
        score += min(_TAG_OVERLAP_CAP, len(tag_overlap) * _TAG_OVERLAP_PER_TOKEN)
        reasons.append(f"tag match: {','.join(sorted(tag_overlap))}")
    if c.category and c.category.lower() in tokens:
        score += _CATEGORY_BONUS
        reasons.append(f"category match: {c.category}")
    return score, reasons


def _score_quality_signals(c: CandidateAgent) -> tuple[float, list[str]]:
    """Pure: success-rate + trust + recommended bonuses for agents with a track record."""
    score = 0.0
    reasons: list[str] = []
    if c.raw.get("call_count", 0) >= _QUALITY_TRACK_RECORD_CALLS:
        score += min(_QUALITY_SUCCESS_BONUS_CAP, c.success_rate * 10)
        score += min(_QUALITY_TRUST_BONUS_CAP, c.trust_score / _TRUST_BONUS_DIVISOR)
    if c.raw.get("codex_recommended"):
        score += _CODEX_RECOMMENDED_BONUS
        reasons.append("recommended")
    return score, reasons


def _detect_audit_signal(intent_lower: str, tokens: set[str]) -> bool:
    """Pure: True if the intent reads like a dependency-audit / CVE check.

    Why: audit/vulnerability intents must dominate over the generic Python
    execution rule — "Check vulnerabilities in my Python project" mentions
    Python but is asking for a dependency audit, not code execution.
    """
    return (
        bool(_AUDIT_TOKEN_SET & tokens)
        or "package.json" in intent_lower
        or "requirements.txt" in intent_lower
        or _looks_like_package_pinning(intent_lower)
    )


def _score_intent_interlocks(
    c: CandidateAgent, intent: str, intent_lower: str,
    tokens: set[str], combined: str, audit_signal: bool,
) -> tuple[float, list[str]]:
    """Pure: cohort-specific bonuses (audit, python-exec, lint, browser, image, financial)."""
    score = 0.0
    reasons: list[str] = []
    is_dependency_agent = (
        any(tok in combined for tok in _DEPENDENCY_AGENT_HINTS)
        or ("dependency" in combined and "audit" in combined)
    )
    if audit_signal and is_dependency_agent:
        score += _DEPENDENCY_AUDIT_BONUS
        reasons.append("dependency audit intent")
    has_strong_exec_verb = bool(_EXEC_VERBS & tokens)
    has_python_token = bool(_PYTHON_TOKENS & tokens)
    looks_codey = ("\n" in intent) or any(token in intent for token in _CODEY_HINTS)
    if (
        ("python" in combined and "executor" in combined)
        and not audit_signal
        and ((has_strong_exec_verb and has_python_token) or looks_codey)
    ):
        score += _INTENT_INTERLOCK_BONUS
        reasons.append("python execution intent")
    if _LINT_TOKENS & tokens and "linter" in combined:
        score += _INTENT_INTERLOCK_BONUS
        reasons.append("lint intent")
    if _BROWSER_TOKENS & tokens and any(t in combined for t in _BROWSER_AGENT_HINTS):
        score += _INTENT_INTERLOCK_BONUS
        reasons.append("browser/screenshot intent")
    if _IMAGE_TOKENS & tokens and any(t in combined for t in _IMAGE_AGENT_HINTS):
        score += _INTENT_INTERLOCK_BONUS
        reasons.append("image generation intent")
    if _FINANCIAL_TOKENS & tokens and any(t in combined for t in _FINANCIAL_AGENT_HINTS):
        score += _INTENT_INTERLOCK_BONUS
        reasons.append("financial filing intent")
    return score, reasons


def _score_keyword_overrides(
    c: CandidateAgent, intent_lower: str,
) -> tuple[float, list[str]]:
    """Pure: curated match/block keyword adjustments — the strongest natural-language signal."""
    score = 0.0
    reasons: list[str] = []
    if c.match_keywords:
        hits = [kw for kw in c.match_keywords if kw and kw in intent_lower]
        if hits:
            score += min(_KEYWORD_MATCH_CAP, len(hits) * _KEYWORD_MATCH_PER)
            reasons.append(f"keyword match: {','.join(hits[:_KEYWORD_PREVIEW_LIMIT])}")
    if c.block_keywords:
        blocks = [kw for kw in c.block_keywords if kw and kw in intent_lower]
        if blocks:
            score -= min(_BLOCK_KEYWORD_CAP, len(blocks) * _BLOCK_KEYWORD_PER)
            reasons.append(f"blocked by: {','.join(blocks[:_KEYWORD_PREVIEW_LIMIT])}")
    return score, reasons


def _collect_composite_required(schema: dict[str, Any]) -> list[list[str]]:
    """Pure: extract per-variant ``required`` lists from oneOf / anyOf composites."""
    out: list[list[str]] = []
    for keyword in ("oneOf", "anyOf"):
        variants = schema.get(keyword)
        if not isinstance(variants, list):
            continue
        for v in variants:
            if not isinstance(v, dict):
                continue
            vreq = list(v.get("required") or [])
            if vreq:
                out.append(vreq)
    return out


def _score_schema_shape(
    c: CandidateAgent, explicit_input: dict[str, Any] | None,
) -> tuple[float, list[str]]:
    """Pure: schema-shape disambiguator — highest-signal when caller passes structured input.

    Why: intent-string-only routing can't tell "lint this Python" → linter
    from python_code_executor; presence of all required keys gives a
    deterministic +35 bump that breaks ties.
    """
    if not isinstance(explicit_input, dict) or not c.input_schema:
        return 0.0, []
    required = list(c.input_schema.get("required") or [])
    if required:
        present = [f for f in required if f in explicit_input]
        if len(present) == len(required):
            return _SCHEMA_SHAPE_FULL_BONUS, ["schema-shape match (all required)"]
        if present:
            return _SCHEMA_SHAPE_PARTIAL_BONUS, [
                f"schema-shape partial ({len(present)}/{len(required)})"
            ]
        return 0.0, []
    composite = _collect_composite_required(c.input_schema)
    if not composite:
        return 0.0, []
    if any(all(f in explicit_input for f in vreq) for vreq in composite):
        return _SCHEMA_SHAPE_FULL_BONUS, ["schema-shape match (composite variant)"]
    best = max(composite, key=lambda vr: sum(1 for f in vr if f in explicit_input))
    n_present = sum(1 for f in best if f in explicit_input)
    if not n_present:
        return 0.0, []
    return _SCHEMA_SHAPE_PARTIAL_BONUS, [
        f"schema-shape partial (composite {n_present}/{len(best)})"
    ]


def _apply_probation_penalty(c: CandidateAgent) -> tuple[float, list[str]]:
    """Pure: probation listings get a fixed rank penalty so they never top generic intents.

    Why: the penalty never zeroes the score, so explicit slug/keyword
    matches still surface a probation listing when a caller asks for it
    by name. Graduates to no penalty once review_status is 'approved'.
    """
    if str(c.raw.get("review_status") or "").strip() != "probation":
        return 0.0, []
    return -_PROBATION_RANK_PENALTY, ["probation: ranked last"]


def _candidate_combined_text(c: CandidateAgent) -> str:
    """Pure: lowercased haystack across slug/name/description/tags for substring checks."""
    return " ".join([
        c.slug.lower(), c.name.lower(), c.description.lower(),
        " ".join(c.tags).lower(),
    ])


def _score_candidate(
    c: CandidateAgent,
    intent: str,
    explicit_input: dict[str, Any] | None = None,
) -> Ranked:
    """Pure: lean confidence-oriented scorer; sums independent signal helpers.

    Why: each helper covers one signal class (string overlap, quality,
    intent interlocks, curated keywords, schema-shape, probation) so the
    score can be tuned per-class without touching the orchestrator.
    """
    intent_lower = intent.lower()
    tokens = set(_tokenize(intent_lower))
    if not tokens:
        return Ranked(candidate=c, score=0.0)
    combined = _candidate_combined_text(c)
    audit_signal = _detect_audit_signal(intent_lower, tokens)
    score = 0.0
    reasons: list[str] = []
    for delta, why in (
        _score_string_signals(c, intent_lower, tokens),
        _score_quality_signals(c),
        _score_intent_interlocks(c, intent, intent_lower, tokens, combined, audit_signal),
        _score_keyword_overrides(c, intent_lower),
        _score_schema_shape(c, explicit_input),
        _apply_probation_penalty(c),
    ):
        score += delta
        reasons.extend(why)
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


def _collect_composite_variants(schema: dict) -> list[list[str]]:
    """Pure: per-variant ``required`` lists from ``oneOf``/``anyOf``.

    Why: ``allOf`` is intentionally excluded — its semantics require *all*
    sub-schemas simultaneously, not one variant. No built-in uses it for
    input gating.
    """
    variants_out: list[list[str]] = []
    for keyword in ("oneOf", "anyOf"):
        variants = schema.get(keyword)
        if not isinstance(variants, list):
            continue
        for variant in variants:
            if isinstance(variant, dict):
                vreq = list(variant.get("required") or [])
                if vreq:
                    variants_out.append(vreq)
    return variants_out


def _resolve_explicit_input(
    explicit_input: dict[str, Any], required: list[str],
    composite_variants: list[list[str]],
) -> tuple[dict[str, Any], list[str]]:
    """Pure: validate caller-supplied payload against top-level + composite required fields."""
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
    return explicit_input, []


def _resolve_intent_only_payload(
    intent: str, all_required: list[str],
    properties: dict, composite_variants: list[list[str]],
) -> tuple[dict[str, Any], list[str]]:
    """Pure: auto-fill from intent when safe; refuse to force-fit chat into code/sql/etc.

    Why: a conversational question must not be force-fitted into a code /
    sql / manifest / diff field; without this gate, aztea_do would route
    "what is the capital of France" to python_code_executor as ``code``.

    1.6.2: for fields where a conversational dump is never valid
    (``expression``, ``pattern``, ``domain``, ``cve_id``, ``url``, …) we
    consult a per-field extractor registry. If the extractor confidently
    pulls a value out of the natural-language intent, we use it; otherwise
    we refuse with ``missing_fields`` so the caller can resubmit with an
    explicit ``input=``. This closes the 1.6.1 P1 where ``do_specialist_task
    ("whats the cron for every weekday at 9am")`` auto-hired
    ``cron_expression_parser`` with the entire 9-word sentence as the
    ``expression`` field — the agent rejected with "Expected 5 or 6 fields,
    got 9" and we burned a refund round-trip every time.
    """
    # No required fields AND no composite variants → free-form intent.
    if not all_required and not composite_variants:
        return {"intent": intent}, []
    if len(all_required) == 1 and not composite_variants:
        field_name = all_required[0]
        field_spec = properties.get(field_name) or {}
        field_type = str(field_spec.get("type") or "").lower()
        if field_type in {"string", ""}:
            # Try a strict-form extractor first. When the field has one and
            # it matches confidently, use the extracted value; when it has
            # one but doesn't match, refuse rather than dump the intent.
            extractor = _FIELD_EXTRACTORS.get(field_name)
            if extractor is not None:
                extracted = extractor(intent)
                if extracted:
                    return {field_name: extracted}, []
                return {}, [field_name]
            if _intent_unfit_for_field(intent, field_name):
                return {}, [field_name]
            return {field_name: intent}, []
    # Composite oneOf/anyOf — pick the first variant and try to extract
    # each of its required fields. If we can fill them all, return the
    # extracted payload; otherwise list the unfilled fields as missing.
    if composite_variants:
        variant = composite_variants[0]
        extracted_payload: dict[str, Any] = {}
        unfilled: list[str] = []
        for fname in variant:
            extractor = _FIELD_EXTRACTORS.get(fname)
            if extractor is not None:
                v = extractor(intent)
                if v:
                    extracted_payload[fname] = v
                    continue
            unfilled.append(fname)
        if not unfilled and extracted_payload:
            return extracted_payload, []
        return {}, variant
    return {}, all_required


def _resolve_payload(
    agent: CandidateAgent,
    intent: str,
    explicit_input: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Pure: build the payload or list missing required fields.

    Why: handles both top-level ``required`` and composite
    ``oneOf``/``anyOf`` variants so agents like CVE lookup are gated
    correctly.
    """
    schema = agent.input_schema if isinstance(agent.input_schema, dict) else {}
    required = list(schema.get("required") or [])
    properties = dict(schema.get("properties") or {})
    composite_variants = _collect_composite_variants(schema)
    if explicit_input is not None:
        return _resolve_explicit_input(explicit_input, required, composite_variants)
    all_required = required or (composite_variants[0] if composite_variants else [])
    return _resolve_intent_only_payload(
        intent, all_required, properties, composite_variants,
    )


# ── Helpers ────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text or "") if len(t) > 2]


# Field-name → intent shape gates. If the agent expects a chunk of source
# code, SQL, a diff, or a manifest, a chat-style question is not valid input
# and must be rejected at the auto-hire gate. Without this, aztea_do happily
# routes "what is the capital of France" to python_code_executor as
# ``code: "what is..."`` (caught in the 2026-05-07 eval).
#
# 1.6.2: expanded to include strict-form fields (expression, pattern, cron,
# regex, jmespath, selector, url, domain) so the naive
# `_resolve_intent_only_payload` 1-field path doesn't dump the entire
# natural-language intent into `expression` and trigger
# `cron_expression_parser.invalid_expression: Expected 5 or 6 fields, got 9`.
# When the intent looks like the structured value an extractor pulls it out;
# otherwise we refuse with `missing_fields` and let the caller resubmit with
# an explicit input. Both outcomes preserve the auto-refund contract.
_CODE_LIKE_FIELDS = frozenset(
    {
        "code",
        "sql",
        "diff",
        "manifest",
        "schema_sql",
        "patch",
        "source",
        # 1.6.2: strict-form fields where conversational dumps are never valid.
        "expression",
        "pattern",
        "cron",
        "regex",
        "jmespath",
        "selector",
        "url",
        "domain",
    }
)


# ── per-field NL extractors (1.6.2) ───────────────────────────────────────
#
# Each extractor is a pure function: ``(intent: str) -> str | None``.
# Returns the extracted value if the intent contains an unambiguous
# match; returns ``None`` to refuse (caller falls back to missing_fields).
# Never raises. Order matters — first match wins.

# Cron: 5- or 6-field expression, or @macro.
_CRON_LITERAL_RE = re.compile(
    r"(?<![\w-])((?:\*|\d+|\*/\d+|\d+[/-]\d+|\d+(?:,\d+)+|[A-Z]{3})\s+){4,5}"
    r"(?:\*|\d+|\*/\d+|\d+[/-]\d+|\d+(?:,\d+)+|[A-Z]{3}(?:-[A-Z]{3})?)(?![\w-])",
)
_CRON_MACRO_RE = re.compile(
    r"@(?:hourly|daily|weekly|monthly|yearly|annually|midnight|reboot)\b",
    re.IGNORECASE,
)
# Natural-language cron table — top-N phrasings translated to canonical cron.
# Order: most-specific first. Each entry is (pattern, cron-expression).
_CRON_NL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bevery\s+weekday\s+at\s+(\d{1,2})\s*am\b", re.IGNORECASE), "0 {h} * * 1-5"),
    (re.compile(r"\bevery\s+weekday\s+at\s+(\d{1,2})\s*pm\b", re.IGNORECASE), "0 {h_pm} * * 1-5"),
    (re.compile(r"\bevery\s+weekday\s+at\s+(\d{1,2}):(\d{2})\b", re.IGNORECASE), "{m_min} {h_hr} * * 1-5"),
    (re.compile(r"\bevery\s+weekend\b", re.IGNORECASE), "0 0 * * 6,0"),
    (re.compile(r"\bevery\s+day\s+at\s+(\d{1,2})\s*am\b", re.IGNORECASE), "0 {h} * * *"),
    (re.compile(r"\bevery\s+day\s+at\s+(\d{1,2})\s*pm\b", re.IGNORECASE), "0 {h_pm} * * *"),
    (re.compile(r"\bevery\s+day\s+at\s+(\d{1,2}):(\d{2})\b", re.IGNORECASE), "{m_min} {h_hr} * * *"),
    (re.compile(r"\bevery\s+(\d+)\s+minutes?\b", re.IGNORECASE), "*/{n} * * * *"),
    (re.compile(r"\bevery\s+(\d+)\s+hours?\b", re.IGNORECASE), "0 */{n} * * *"),
    (re.compile(r"\bevery\s+hour\b", re.IGNORECASE), "0 * * * *"),
    (re.compile(r"\bdaily\b", re.IGNORECASE), "0 0 * * *"),
    (re.compile(r"\bhourly\b", re.IGNORECASE), "0 * * * *"),
)


def _extract_cron_expression(intent: str) -> str | None:
    """Pull a cron expression out of natural-language intent. Returns the
    canonical string, or None if extraction is not confident."""
    text = intent or ""
    if not text:
        return None
    # 1. Literal cron (5 or 6 fields).
    m = _CRON_LITERAL_RE.search(text)
    if m:
        return m.group(0).strip()
    # 2. @macro form.
    m = _CRON_MACRO_RE.search(text)
    if m:
        return m.group(0).lower()
    # 3. Natural-language patterns.
    for pat, template in _CRON_NL_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        groups = m.groups()
        if "{m_min}" in template and "{h_hr}" in template and len(groups) >= 2:
            # H:M form — group 0 is the hour, group 1 is the minute.
            try:
                return template.format(h_hr=int(groups[0]), m_min=int(groups[1]))
            except ValueError:
                return None
        if "{h_pm}" in template and groups:
            try:
                h = int(groups[0])
                # 12pm -> 12, 1pm-11pm -> +12
                return template.format(h_pm=12 if h == 12 else h + 12)
            except ValueError:
                return None
        if "{h}" in template and groups:
            try:
                # 12am -> 0, 1am-11am -> as-is
                h = int(groups[0])
                return template.format(h=0 if h == 12 else h)
            except ValueError:
                return None
        if "{n}" in template and groups:
            try:
                return template.format(n=int(groups[0]))
            except ValueError:
                return None
        return template
    return None


# Regex pattern: between slashes, backticks, or after the literal word.
_REGEX_SLASH_RE = re.compile(r"/([^/\n]{1,200})/")
_REGEX_BACKTICK_RE = re.compile(r"`([^`\n]{1,200})`")
_REGEX_QUOTED_RE = re.compile(r"(?:pattern|regex|match)\s+[\"']([^\"'\n]{1,200})[\"']", re.IGNORECASE)


def _extract_regex_pattern(intent: str) -> str | None:
    text = intent or ""
    if not text:
        return None
    # Backticks first (commonly used to delimit code-y values in chat).
    m = _REGEX_BACKTICK_RE.search(text)
    if m:
        return m.group(1)
    # Slashes (literal /pattern/ form).
    m = _REGEX_SLASH_RE.search(text)
    if m:
        return m.group(1)
    # Quoted after "pattern"/"regex"/"match".
    m = _REGEX_QUOTED_RE.search(text)
    if m:
        return m.group(1)
    # Backslash-escape style: "\d+", "[a-z]+" — bare regex tokens are
    # common in casual usage. Match if the intent contains *only*
    # regex-shaped characters around it.
    bare = re.search(r"(?<![\w/`])(\\[dDwWsSbBnrtv]\S*|\[[^\]\n]{1,80}\][?*+]?)", text)
    if bare:
        return bare.group(1)
    return None


_DOMAIN_RE = re.compile(
    r"\b([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?){1,4})\b",
    re.IGNORECASE,
)
_PRIVATE_IP_RE = re.compile(
    r"\b(?:127\.|10\.|192\.168\.|169\.254\.|0\.0\.0\.0|localhost|::1)\b",
    re.IGNORECASE,
)


def _extract_domain(intent: str) -> str | None:
    text = intent or ""
    if not text:
        return None
    # Reject private/loopback intents outright — never auto-route into a
    # live-network agent. SSRF guards exist downstream too, but failing
    # here saves a charge round-trip.
    if _PRIVATE_IP_RE.search(text):
        return None
    m = _DOMAIN_RE.search(text)
    if not m:
        return None
    candidate = m.group(1)
    # TLD must be alphabetic to avoid matching "foo.123" or version-like
    # tokens. Slight false-negative cost but very low false-positive rate.
    tld = candidate.rsplit(".", 1)[-1]
    if not tld.isalpha() or len(tld) < 2:
        return None
    return candidate.lower()


_CVE_ID_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)


def _extract_cve_id(intent: str) -> str | None:
    m = _CVE_ID_RE.search(intent or "")
    return m.group(0).upper() if m else None


_URL_RE = re.compile(r"\bhttps?://[^\s\"'<>]{4,2000}", re.IGNORECASE)


def _extract_url(intent: str) -> str | None:
    text = intent or ""
    if not text or _PRIVATE_IP_RE.search(text):
        return None
    m = _URL_RE.search(text)
    return m.group(0) if m else None


# Registry: field-name → extractor. ``_resolve_intent_only_payload`` looks
# up the field here before falling back to the legacy "dump intent into
# field" behaviour. Returning None refuses with `missing_fields`.
_FIELD_EXTRACTORS: dict[str, "Callable[[str], str | None]"] = {
    "expression": _extract_cron_expression,
    "cron": _extract_cron_expression,
    "pattern": _extract_regex_pattern,
    "regex": _extract_regex_pattern,
    "domain": _extract_domain,
    "cve_id": _extract_cve_id,
    "url": _extract_url,
}
_QUESTION_PREFIXES = (
    "what ",
    "what's ",
    "whats ",
    "who ",
    "why ",
    "when ",
    "where ",
    "how ",
    "can ",
    "could ",
    "would ",
    "should ",
    "is ",
    "are ",
    "do ",
    "does ",
    "did ",
    "will ",
    "tell me ",
    "explain ",
    "describe ",
    "summarize ",
    "summarise ",
)


def _looks_like_question(intent: str) -> bool:
    text = (intent or "").strip().lower()
    if not text:
        return False
    if text.endswith("?"):
        return True
    return any(text.startswith(prefix) for prefix in _QUESTION_PREFIXES)


_PACKAGE_PIN_RE = re.compile(
    r"\b[a-z][a-z0-9_.-]{1,40}(?:==|@)\d[\w.\-+]*",
    re.IGNORECASE,
)
# "Check ...", "Audit ...", "Review ...", "Find ..." used as imperatives at the
# start of an intent are conversational asks — never raw code, SQL, manifest, or
# diff content. Matters for the ``_intent_unfit_for_field`` gate so aztea_do
# does not force-fit "Check vulnerabilities in requests==2.25.0" into a
# code-shaped field.
_IMPERATIVE_PREFIXES = (
    "check ",
    "audit ",
    "review ",
    "find ",
    "look up ",
    "lookup ",
    "scan ",
    "investigate ",
    "analyze ",
    "analyse ",
    "evaluate ",
    "assess ",
    "diagnose ",
)

_CODE_TOKENS = (
    "def ", "class ", "import ", "->", "=>", "{", "};", "</", "/>",
    "select ", "SELECT ", "from ", "FROM ",
)


def _looks_like_package_pinning(intent: str) -> bool:
    """True when the intent contains pinned package references like
    ``requests==2.25.0`` or ``axios@1.6.0``. Used by both the auto-hire
    routing rule (audit intents must beat python-execution) and the
    field-fit gate (don't shove a sentence with package pins into a `code`
    field).
    """
    text = intent or ""
    if not text:
        return False
    return _PACKAGE_PIN_RE.search(text) is not None


def _looks_like_code(intent: str) -> bool:
    text = intent or ""
    if not text.strip():
        return False
    # Heuristic: real code has multiple lines, common code punctuation,
    # or recognizable keywords/operators that don't appear in chat.
    if "\n" in text:
        return True
    return any(token in text for token in _CODE_TOKENS)


def _intent_unfit_for_field(intent: str, field_name: str) -> bool:
    """Return True when the intent string is obviously the wrong shape for
    the named field. Conservative — only blocks the clear-cut cases.
    """
    if not field_name:
        return False
    name = field_name.lower()
    if name not in _CODE_LIKE_FIELDS:
        return False
    if _looks_like_code(intent):
        return False
    if _looks_like_question(intent):
        return True
    text = (intent or "").strip().lower()
    if any(text.startswith(prefix) for prefix in _IMPERATIVE_PREFIXES):
        return True
    if _looks_like_package_pinning(intent):
        # Sentences with `requests==2.25.0` style pins are about packages,
        # not raw code blocks. Force the caller to use structured input or
        # to retry with a different intent.
        return True
    return False


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
