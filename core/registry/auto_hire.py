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
            next_step="Use search_specialists + call_specialist directly.",
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
                "Multiple agents could fit. Call describe_specialist on a candidate, "
                "then call_specialist to run it."
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
                f"Top match {top.candidate.slug!r} is in beta. Call call_specialist "
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
                f"max_cost_usd to at least ${price:.2f}, or call call_specialist "
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

    combined = " ".join([c.slug.lower(), c.name.lower(), c.description.lower(), " ".join(c.tags).lower()])
    intent_lower_full = intent.lower()
    # Audit/vulnerability/dependency intents must dominate over the generic
    # python-execution rule. A user saying "Check vulnerabilities in my Python
    # project, requests==2.25.0 pyyaml==5.3.1" mentions the word "python" but
    # is NOT asking us to execute code — they want a dependency audit. Caught
    # in the 2026-05-08 eval where aztea_do routed this exact intent to
    # python_code_executor with confidence 0.97 and passed the natural-
    # language string as `code`, producing a $0.01 SyntaxError.
    audit_signal = (
        bool({"audit", "audits", "auditing", "vulnerability", "vulnerabilities", "cve", "cves", "supply"} & tokens)
        or "package.json" in intent_lower_full
        or "requirements.txt" in intent_lower_full
        or _looks_like_package_pinning(intent_lower_full)
    )
    is_dependency_agent = any(
        tok in combined for tok in ("dependency_auditor", "dependency auditor", "dep-audit")
    ) or ("dependency" in combined and "audit" in combined)
    if audit_signal and is_dependency_agent:
        score += 70
        reasons.append("dependency audit intent")

    # Python execution intent: require a strong execution verb AND no audit
    # signal. The verb-only check ("python" in tokens) was too loose — every
    # mention of Python the language fired the +45 bonus, including the user
    # describing what stack their PROJECT is in.
    has_strong_exec_verb = bool(
        {"run", "execute", "evaluate", "repl", "interpreter", "compute"} & tokens
    )
    has_python_token = bool({"python", "py3", "python3"} & tokens)
    looks_codey = ("\n" in intent) or any(
        token in intent for token in ("def ", "class ", "import ", "print(", "lambda ")
    )
    python_exec_match = (
        "python" in combined and "executor" in combined
    )
    if python_exec_match and not audit_signal and (
        (has_strong_exec_verb and has_python_token) or looks_codey
    ):
        score += 45
        reasons.append("python execution intent")
    if {"lint", "linter", "ruff", "eslint"} & tokens and "linter" in combined:
        score += 45
        reasons.append("lint intent")
    if {"browser", "screenshot", "playwright", "homepage"} & tokens and any(
        token in combined for token in ("browser", "playwright", "screenshot")
    ):
        score += 45
        reasons.append("browser/screenshot intent")
    if {"image", "generate", "generation", "dall", "replicate"} & tokens and any(
        token in combined for token in ("image", "generation", "replicate", "gpt-image")
    ):
        score += 45
        reasons.append("image generation intent")
    if {"edgar", "10-k", "sec", "revenue"} & tokens and any(
        token in combined for token in ("edgar", "sec", "financial")
    ):
        score += 45
        reasons.append("financial filing intent")

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

    # Single top-level string field: auto-fill from intent — but only when
    # the intent looks like the kind of input the agent expects. A
    # conversational question like "what is the capital of France" must
    # never be force-fitted into a `code`, `sql`, `manifest`, or `diff`
    # field; that produced obvious garbage in the 2026-05-07 eval where
    # python_code_executor was hired with the question as its `code`.
    if len(all_required) == 1 and not composite_variants:
        field_name = all_required[0]
        field_spec = properties.get(field_name) or {}
        field_type = str(field_spec.get("type") or "").lower()
        if field_type in {"string", ""}:
            if _intent_unfit_for_field(intent, field_name):
                return {}, [field_name]
            return {field_name: intent}, []

    # Composite schema without explicit_input: cannot auto-fill structured fields.
    if composite_variants:
        return {}, composite_variants[0]

    return {}, all_required


# ── Helpers ────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text or "") if len(t) > 2]


# Field-name → intent shape gates. If the agent expects a chunk of source
# code, SQL, a diff, or a manifest, a chat-style question is not valid input
# and must be rejected at the auto-hire gate. Without this, aztea_do happily
# routes "what is the capital of France" to python_code_executor as
# ``code: "what is..."`` (caught in the 2026-05-07 eval).
_CODE_LIKE_FIELDS = frozenset(
    {"code", "sql", "diff", "manifest", "schema_sql", "patch", "source"}
)
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
    if any(token in text for token in ("def ", "class ", "import ", "->", "=>", "{", "};", "</", "/>", "select ", "SELECT ", "from ", "FROM ")):
        return True
    return False


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
