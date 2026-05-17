"""Agent registry operations: writes, reads, search, and reputation enrichment.

Paired with ``core.registry.core_schema`` (schema + low-level helpers). This
module implements everything the HTTP layer needs on top of the raw schema:

- ``register_agent`` / ``update_agent`` / ``delete_agent`` — creation, mutation,
  and soft-delete flows with validation hooks.
- ``get_agent`` / ``get_agents`` / ``count_owner_agents`` — read paths with
  built-in filters for visibility (banned / unapproved), ownership, tags,
  model provider, and rank-by mode.
- ``search_agents`` — semantic + substring search layered over the sentence
  transformer embeddings in ``core.embeddings``. Falls back gracefully when
  the embedding model is not available.
- ``get_agent_with_reputation`` / ``reputation-enriched listing helpers`` —
  attach ``trust_score``, ``success_rate``, ``quality_rating_avg``,
  ``dispute_rate``, and latency stats pulled from the reputation tables.
- **Review + moderation.** ``list_pending_review_agents``,
  ``set_agent_review_decision`` (approve / reject) and the auto-verification
  flow that runs ``output_verifier_url`` during registration.
- **Endpoint health monitoring.** ``set_agent_endpoint_health``, degraded
  / recovered transitions, and the counters feeding the sweeper.

Tests monkeypatch ``core.registry.embeddings`` to stub out network calls to
the sentence-transformer model, so the module keeps ``embeddings`` accessible
as an attribute on the package (see ``core/registry/__init__.py``).
"""

from __future__ import annotations

import json
import math
import re

from core import db as _db
from core.functional import Err, Ok, Result
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np

from core import embeddings
from core import feature_flags as _feature_flags

from .call_history import append_call_ring_sample
from .core_schema import (
    _CANONICAL_CREATED_AT,
    _QUERY_STOP_WORDS,
    _TRUST_PERCENT_SCALE,
    HEALTH_SUSPENSION_THRESHOLD,
    REVIEW_STATUSES,
    _build_embedding_source_text,
    _conn,
    _embedding_source_from_agent,
    _invalidate_embeddings_cache,
    _load_embeddings_for_agents,
    _logger,
    _parse_input_schema,
    _parse_output_schema,
    _parse_tags,
    _row_to_dict,
    _to_non_negative_float,
    _to_non_negative_int,
    _upsert_agent_embedding_row,
)
from .pricing import (
    VariablePricingError,
    normalize_pricing_model,
    validate_pricing_config,
)

# Semantic above lexical: lexical-only ranking produced the eval's wrong-intent
# bugs (JWT decode → visual_regression because both share "base64", screenshot
# a website → visual_regression over browser_agent because of the "screenshot"
# token). Semantic similarity (sentence-transformers / OpenAI embeddings)
# captures intent the way lexical overlap can't. Lexical retains 0.30 as a
# tiebreaker so exact-slug queries still resolve fast.
LEXICAL_SCORE_WEIGHT = 0.30
SEMANTIC_SCORE_WEIGHT = 0.50
TRUST_SCORE_WEIGHT_HYBRID = 0.12
INVERSE_PRICE_WEIGHT_HYBRID = 0.08

# Confidence thresholds applied AFTER ranking. Tuned empirically against the
# 9-agent live catalog so genuinely-on-target queries ("scan code for
# secrets", "look up CVE") still surface results, but adversarial /
# off-catalog queries ("image generator", "find recent papers") return
# empty rather than weak distractors. Defaults match the legacy literals;
# override at runtime via AZTEA_SEARCH_* env vars (see core/feature_flags.py).
# Read on each call so a redeploy isn't needed to retune.

# Off-catalog intent fingerprints. When the query unambiguously asks for a
# capability the catalog does not yet have (research papers, type checking,
# image generation, etc.) we want to surface "no match" with a clear
# explanation instead of returning the highest-scoring distractor. The eval
# flagged "find recent papers on attention mechanisms" → DNS Inspector and
# "test if my endpoint is fast enough" → Visual Regression as confidence-
# destroying. Each entry is (description, predicate-on-tokens).
_OFF_CATALOG_PATTERNS = [
    # NOTE: Adding a pattern here BLOCKS THE QUERY ENTIRELY — even when
    # a matching agent exists. Audit checklist before adding:
    #   1. Confirm NO current agent's input_schema can serve the query.
    #   2. Re-confirm whenever the catalog grows (every PR adding an
    #      agent should grep this list for collisions).
    #   3. Prefer per-agent block_keywords over a global pattern when
    #      the intent is "rank this agent down for this query"
    #      (rather than "no agent should win this query").
    # 2026-05-11 audit removed: "JWT / JOSE token decoding" (we have
    # jwt_debugger), "endpoint latency / load testing" (we have
    # load_tester + lighthouse_auditor), "TypeScript / mypy type-
    # checking" (we have type_checker). Each had been blocking direct
    # hits since the matching agent was added, and an eval session
    # hit them within the first 5 search queries.
    (
        "research papers / academic literature",
        lambda toks: bool(
            {"papers", "paper", "arxiv", "academic", "preprint", "preprints"}
            & toks
        ),
    ),
    (
        "image generation",
        lambda toks: bool(
            # "dall e" splits to {"dall", "e"}; "dall-e" stays as one token;
            # "dalle" is the no-separator spelling. Include all three so the
            # off-catalog pattern catches the canonical brand spellings users
            # actually type.
            {"dall", "dall-e", "dalle", "midjourney", "stable", "diffusion"} & toks
        )
        and ("image" in toks or "picture" in toks),
    ),
    (
        "OWASP guidance / threat-model frameworks (no agent maps OWASP top-10 → finding)",
        lambda toks: "owasp" in toks,
    ),
    (
        "joke / chitchat",
        lambda toks: bool({"joke", "jokes", "funny"} & toks),
    ),
    (
        "cooking / food",
        lambda toks: bool({"cook", "cooking", "dinner", "recipe", "recipes", "food"} & toks),
    ),
    (
        "credit card / payment-card",
        lambda toks: ("credit" in toks and "card" in toks) or ("credit" in toks and "cards" in toks),
    ),
]

# Typo + acronym → canonical-term expansions ONLY. Do not list "code
# execution", "python", "base64", or other generic terms here — every
# expansion you add to a query is a token every candidate's lexical
# overlap can match on, so an expansion of "jwt → ...base64 decode python"
# made visual_regression (image base64) outrank the right answer for
# "JWT decode". Keep these tight and intent-preserving.
_QUERY_EXPANSIONS = {
    "secrt": "secret",
    "scaner": "scanner",
    "linnt": "lint",
    "depndency": "dependency",
    "vuln": "vulnerability",
    "vulns": "vulnerabilities",
    "hardcoded": "hardcoded credential",
    "tls": "ssl certificate",
    "handshake": "ssl tls certificate",
    "ssl": "tls certificate",
    "jwt": "json web token",
    "sbom": "software bill of materials dependency",
    "sca": "software composition analysis dependency",
    "owasp": "web application security vulnerability",
    "ssrf": "server side request forgery url",
    "xss": "cross site scripting",
    "redos": "regex denial of service",
    "rce": "remote code execution",
    "dnssec": "dns certificate",
    "hsts": "http security headers",
    "csp": "content security policy",
    "imds": "metadata service ssrf",
    "10k": "10-k sec edgar filing",
    "10-q": "sec edgar filing",
}

_NON_ENGLISH_QUERY_EXPANSIONS = {
    "检查代码中的漏洞": "scan code vulnerabilities security secret scanner code review",
    "漏洞": "vulnerability security cve",
    "代码": "code",
    "秘密": "secret credential",
    "密钥": "secret key credential",
    "依赖": "dependency package audit",
}


def _expand_search_query(query: str) -> str:
    lowered = str(query or "").strip().lower()
    additions: list[str] = []
    for needle, expansion in _NON_ENGLISH_QUERY_EXPANSIONS.items():
        if needle in query:
            additions.append(expansion)
    for term in re.findall(r"[a-z0-9-]+", lowered):
        expansion = _QUERY_EXPANSIONS.get(term)
        if expansion:
            additions.append(expansion)
    if not additions:
        return str(query or "").strip()
    return " ".join([str(query or "").strip(), *additions]).strip()


def _validate_agent_scalar_params(
    price_per_call_usd: float,
    endpoint_health_status: str,
    status: str,
    review_status: str | None,
    is_internal: bool,
    pricing_model: str | None,
    pricing_config: Any,
) -> "Result[dict, str]":
    """Pure guard for scalar agent params. Returns Ok(normalized_scalars) or Err(message).

    Validates and normalises only the fields that can be checked without DB access.
    The caller substitutes the returned scalars back in, keeping the full registration
    logic in one place.
    """
    try:
        price = float(price_per_call_usd)
    except (TypeError, ValueError):
        return Err("price_per_call_usd must be a non-negative number.")
    if not math.isfinite(price):
        return Err("price_per_call_usd must be a finite non-negative number.")
    if price < 0:
        return Err("price_per_call_usd must be non-negative.")

    normalized_health_status = str(endpoint_health_status or "unknown").strip().lower()
    if normalized_health_status not in {"unknown", "healthy", "degraded"}:
        return Err("endpoint_health_status must be one of: unknown, healthy, degraded.")

    normalized_status = str(status or "active").strip().lower()
    if normalized_status not in {"active", "suspended", "banned"}:
        return Err("status must be one of: active, suspended, banned.")

    _review = str(review_status or "").strip().lower()
    normalized_review_status = _review or (
        "approved" if is_internal else "pending_review"
    )
    if normalized_review_status not in REVIEW_STATUSES:
        return Err(
            "review_status must be one of: " + ", ".join(sorted(REVIEW_STATUSES)) + "."
        )

    try:
        normalized_pricing_model = normalize_pricing_model(pricing_model)
    except VariablePricingError as exc:
        return Err(str(exc))

    pricing_config_json: str | None = None
    if normalized_pricing_model != "fixed":
        try:
            canonical_config = validate_pricing_config(
                normalized_pricing_model, pricing_config
            )
        except VariablePricingError as exc:
            return Err(str(exc))
        if canonical_config is not None:
            pricing_config_json = json.dumps(canonical_config, sort_keys=True)

    return Ok(
        {
            "price": price,
            "normalized_health_status": normalized_health_status,
            "normalized_status": normalized_status,
            "normalized_review_status": normalized_review_status,
            "normalized_pricing_model": normalized_pricing_model,
            "pricing_config_json": pricing_config_json,
        }
    )


_VALID_AGENT_KINDS = frozenset({"aztea_built", "community_skill", "self_hosted"})
_AGENT_DISPLAY_LABEL_CHARS = 80
_AGENT_MODEL_ID_CHARS = 128

_REGISTER_AGENT_INSERT_SQL = """
    INSERT INTO agents
        (agent_id, owner_id, name, description, endpoint_url, healthcheck_url,
         price_per_call_usd, tags, input_schema, output_schema, output_verifier_url,
         output_examples, verified, endpoint_health_status, endpoint_consecutive_failures,
         endpoint_last_checked_at, endpoint_last_error,
         internal_only, status, review_status, review_note, reviewed_at, reviewed_by,
         trust_decay_multiplier, last_decay_at, created_at,
         model_provider, model_id, pricing_model, pricing_config, kind,
         pii_safe, outputs_not_stored, audit_logged, region_locked, payout_curve, cacheable)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, NULL, NULL,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_REGISTER_AGENT_SIGNING_UPDATE_SQL = """
    UPDATE agents
    SET did = %s,
        signing_public_key = %s,
        signing_private_key = %s,
        signing_keys_created_at = %s
    WHERE agent_id = %s
"""


def _normalize_register_scalars(
    *, price_per_call_usd: float, endpoint_health_status: str,
    status: str, review_status: str | None, is_internal: bool,
    pricing_model: str | None, pricing_config: dict | None,
) -> dict[str, Any]:
    """Pure: validate scalar agent params at the boundary and return canonical values.

    Why: every malformed input becomes a single ``ValueError``; the DB
    insert never sees an inconsistent row.
    """
    _scalars = _validate_agent_scalar_params(
        price_per_call_usd, endpoint_health_status, status,
        review_status, is_internal, pricing_model, pricing_config,
    )
    _scalars.raise_on_err()
    return _scalars.value


def _normalize_register_optional_strings(
    *, healthcheck_url: str | None, output_verifier_url: str | None,
    review_note: str | None, reviewed_at: str | None,
    reviewed_by: str | None, region_locked: str | None,
    model_provider: str | None, model_id: str | None,
) -> dict[str, str | None]:
    """Pure: strip + collapse-empty for every optional string field on the agent row."""
    return {
        "healthcheck_url": str(healthcheck_url or "").strip() or None,
        "verifier_url": str(output_verifier_url or "").strip() or None,
        "review_note": str(review_note or "").strip() or None,
        "reviewed_at": str(reviewed_at or "").strip() or None,
        "reviewed_by": str(reviewed_by or "").strip() or None,
        "region_locked": str(region_locked or "").strip().lower() or None,
        "model_provider": str(model_provider).strip().lower() if model_provider else None,
        "model_id": str(model_id).strip()[:_AGENT_MODEL_ID_CHARS] if model_id else None,
    }


def _normalize_examples_json(output_examples: list | None) -> str | None:
    """Pure: filter to dict-only examples and serialise; empty list / non-list → None."""
    if not isinstance(output_examples, list):
        return None
    encoded = json.dumps([ex for ex in output_examples if isinstance(ex, dict)])
    return encoded or None


def _coerce_decay_multiplier(trust_decay_multiplier: float) -> float:
    """Pure: enforce decay > 0; coerce zero/negative to 1.0 (the no-decay default)."""
    value = _to_non_negative_float(trust_decay_multiplier, default=1.0)
    return value if value > 0 else 1.0


def _normalize_agent_kind(kind: str) -> str:
    """Pure: clamp ``kind`` to a known agent-kind value, defaulting to self_hosted."""
    candidate = str(kind or "self_hosted").strip().lower()
    return candidate if candidate in _VALID_AGENT_KINDS else "self_hosted"


def _maybe_embed_listing(
    *, embed_listing: bool, name: str, description: str,
    normalized_tags: list, normalized_schema: dict,
) -> tuple[str, list[float] | None]:
    """Side-effect: compute embedding for new listings; ``("", None)`` when disabled."""
    if not embed_listing:
        return "", None
    source_text = _build_embedding_source_text(
        name, description, normalized_tags, normalized_schema,
    )
    return source_text, embeddings.embed_text(source_text)


def _generate_signing_keypair_safe(
    aid: str, created_at: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Side-effect: best-effort keypair generation.

    Why: missing ``cryptography`` lib in test envs must not block agent
    registration; the agent simply has no signing key until the next
    startup backfill recovers.
    """
    try:
        from core import crypto as _crypto
        from core.identity import build_agent_did as _build_agent_did

        private_pem, public_pem = _crypto.generate_signing_keypair()
        return _build_agent_did(aid), private_pem, public_pem, created_at
    except Exception:
        _logger.exception("Failed to generate signing keypair for agent %s", aid)
        return None, None, None, None


def _persist_signing_keypair(
    conn: Any, *, aid: str, agent_did: str, public_pem: str,
    private_pem: str, signing_keys_created_at: str,
) -> None:
    """Side-effect: write DID + keypair onto the agents row.

    Why: tolerates schemas that pre-date migration 0015 — the column may
    not exist yet, but the next startup backfill will retry.
    """
    try:
        conn.execute(
            _REGISTER_AGENT_SIGNING_UPDATE_SQL,
            (agent_did, public_pem, private_pem, signing_keys_created_at, aid),
        )
    except _db.OperationalError as exc:
        _logger.warning(
            "Could not persist signing keypair for agent %s "
            "(schema not yet migrated?): %s",
            aid, exc,
        )


def _eagerly_create_agent_wallet(
    aid: str, normalized_owner_id: str, name: str,
) -> None:
    """Side-effect: create the agent's payout sub-wallet linked to its human owner.

    Why: best-effort — the job-creation path also calls
    ``get_or_create_wallet`` and recovers if this eager step failed.
    """
    try:
        from core import payments as _payments

        parent_wallet_id: str | None = None
        if normalized_owner_id and normalized_owner_id != f"agent:{aid}":
            owner_wallet = _payments.get_or_create_wallet(normalized_owner_id)
            parent_wallet_id = owner_wallet["wallet_id"]
        _payments.get_or_create_wallet(
            f"agent:{aid}",
            parent_wallet_id=parent_wallet_id,
            display_label=name[:_AGENT_DISPLAY_LABEL_CHARS] if name else None,
        )
    except Exception:
        _logger.exception("failed to eagerly create agent sub-wallet for %s", aid)


def _build_register_agent_params(
    *, aid: str, normalized_owner_id: str, name: str, description: str,
    endpoint_url: str, scalars: dict, optionals: dict,
    tags_json: str, schema_json: str, output_schema_json: str,
    examples_json: str | None, verified: bool, internal_only: bool,
    pii_safe: bool, outputs_not_stored: bool, audit_logged: bool,
    cacheable: bool | None, decay_multiplier: float,
    payout_curve_json: str, kind: str, created_at: str,
) -> tuple:
    """Pure: positional args for ``_REGISTER_AGENT_INSERT_SQL``."""
    return (
        aid,
        normalized_owner_id,
        name,
        description,
        endpoint_url,
        optionals["healthcheck_url"],
        scalars["price"],
        tags_json,
        schema_json,
        output_schema_json,
        optionals["verifier_url"],
        examples_json,
        1 if verified else 0,
        scalars["normalized_health_status"],
        1 if internal_only else 0,
        scalars["normalized_status"],
        scalars["normalized_review_status"],
        optionals["review_note"],
        optionals["reviewed_at"],
        optionals["reviewed_by"],
        decay_multiplier,
        created_at,
        created_at,
        optionals["model_provider"],
        optionals["model_id"],
        scalars["normalized_pricing_model"],
        scalars["pricing_config_json"],
        kind,
        1 if pii_safe else 0,
        1 if outputs_not_stored else 0,
        1 if audit_logged else 0,
        optionals["region_locked"],
        payout_curve_json,
        None if cacheable is None else (1 if cacheable else 0),
    )


def _execute_register_agent_insert(
    conn: Any, *, params: tuple, embed_listing: bool, aid: str,
    source_text: str, embedding_vector: list[float] | None,
    agent_did: str | None, private_pem: str | None,
    public_pem: str | None, signing_keys_created_at: str | None,
) -> None:
    """Side-effect: INSERT the agent row + optional embedding + optional signing keypair."""
    conn.execute(_REGISTER_AGENT_INSERT_SQL, params)
    if embed_listing and embedding_vector is not None:
        _upsert_agent_embedding_row(
            conn, agent_id=aid, source_text=source_text,
            embedding_vector=embedding_vector,
        )
    if agent_did is not None and private_pem is not None:
        _persist_signing_keypair(
            conn, aid=aid, agent_did=agent_did, public_pem=public_pem,
            private_pem=private_pem,
            signing_keys_created_at=signing_keys_created_at,
        )


def register_agent(
    name: str,
    description: str,
    endpoint_url: str,
    price_per_call_usd: float,
    tags: list,
    healthcheck_url: str | None = None,
    agent_id: str | None = None,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    output_verifier_url: str | None = None,
    output_examples: list | None = None,
    verified: bool = False,
    endpoint_health_status: str = "unknown",
    internal_only: bool = False,
    status: str = "active",
    review_status: str | None = None,
    review_note: str | None = None,
    reviewed_at: str | None = None,
    reviewed_by: str | None = None,
    trust_decay_multiplier: float = 1.0,
    owner_id: str | None = None,
    embed_listing: bool = True,
    model_provider: str | None = None,
    model_id: str | None = None,
    pricing_model: str | None = None,
    pricing_config: dict | None = None,
    kind: str = "self_hosted",
    pii_safe: bool = False,
    outputs_not_stored: bool = False,
    audit_logged: bool = False,
    region_locked: str | None = None,
    payout_curve: dict | None = None,
    cacheable: bool | None = None,
) -> str:
    """Side-effect: insert a new agent listing; returns the agent_id.

    Why: passing ``agent_id`` explicitly produces deterministic IDs (e.g.
    self-registration); ``embed_listing=True`` writes an embedding row in
    the same request so search ranking stays current. Raises
    ``_db.IntegrityError`` if ``agent_id`` already exists.
    """
    aid = agent_id or str(uuid.uuid4())
    normalized_owner_id = (owner_id or f"agent:{aid}").strip()
    if not normalized_owner_id:
        raise ValueError("owner_id must be a non-empty string.")
    is_internal = internal_only or str(endpoint_url or "").strip().startswith("internal://")
    scalars = _normalize_register_scalars(
        price_per_call_usd=price_per_call_usd,
        endpoint_health_status=endpoint_health_status,
        status=status, review_status=review_status, is_internal=is_internal,
        pricing_model=pricing_model, pricing_config=pricing_config,
    )
    optionals = _normalize_register_optional_strings(
        healthcheck_url=healthcheck_url, output_verifier_url=output_verifier_url,
        review_note=review_note, reviewed_at=reviewed_at, reviewed_by=reviewed_by,
        region_locked=region_locked, model_provider=model_provider, model_id=model_id,
    )
    created_at = datetime.now(timezone.utc).isoformat()
    normalized_tags = _parse_tags(tags)
    normalized_schema = _parse_input_schema(input_schema)
    normalized_output_schema = _parse_output_schema(output_schema)
    from core import payout_curve as _pc
    payout_curve_json = _pc.curve_to_json(_pc.parse_curve(payout_curve))
    source_text, embedding_vector = _maybe_embed_listing(
        embed_listing=embed_listing, name=name, description=description,
        normalized_tags=normalized_tags, normalized_schema=normalized_schema,
    )
    agent_did, private_pem, public_pem, signing_keys_created_at = (
        _generate_signing_keypair_safe(aid, created_at)
    )
    params = _build_register_agent_params(
        aid=aid, normalized_owner_id=normalized_owner_id, name=name,
        description=description, endpoint_url=endpoint_url, scalars=scalars,
        optionals=optionals,
        tags_json=json.dumps(normalized_tags),
        schema_json=json.dumps(normalized_schema, sort_keys=True),
        output_schema_json=json.dumps(normalized_output_schema, sort_keys=True),
        examples_json=_normalize_examples_json(output_examples),
        verified=verified, internal_only=internal_only, pii_safe=pii_safe,
        outputs_not_stored=outputs_not_stored, audit_logged=audit_logged,
        cacheable=cacheable,
        decay_multiplier=_coerce_decay_multiplier(trust_decay_multiplier),
        payout_curve_json=payout_curve_json, kind=_normalize_agent_kind(kind),
        created_at=created_at,
    )
    with _conn() as conn:
        _execute_register_agent_insert(
            conn, params=params, embed_listing=embed_listing, aid=aid,
            source_text=source_text, embedding_vector=embedding_vector,
            agent_did=agent_did, private_pem=private_pem, public_pem=public_pem,
            signing_keys_created_at=signing_keys_created_at,
        )
    if embed_listing:
        _invalidate_embeddings_cache()
    _eagerly_create_agent_wallet(aid, normalized_owner_id, name)
    return aid


def set_agent_pricing(
    agent_id: str,
    *,
    pricing_model: str,
    pricing_config: dict | None,
) -> dict | None:
    """Update the pricing model/config columns for an existing agent.

    Returns the refreshed agent row, or ``None`` if no agent with
    ``agent_id`` exists.
    """
    try:
        normalized_model = normalize_pricing_model(pricing_model)
    except VariablePricingError as exc:
        raise ValueError(str(exc))
    serialized: str | None = None
    if normalized_model != "fixed":
        try:
            canonical = validate_pricing_config(normalized_model, pricing_config)
        except VariablePricingError as exc:
            raise ValueError(str(exc))
        if canonical is not None:
            serialized = json.dumps(canonical, sort_keys=True)
    with _conn() as conn:
        updated = conn.execute(
            """
            UPDATE agents
            SET pricing_model = %s, pricing_config = %s
            WHERE agent_id = %s
            """,
            (normalized_model, serialized, agent_id),
        ).rowcount
    if updated == 0:
        return None
    return get_agent(agent_id, include_unapproved=True)


def update_call_stats(
    agent_id: str, latency_ms: float, success: bool, *, price_cents: int | None = None
) -> None:
    """
    Increment total_calls, update running avg_latency_ms, and conditionally
    increment successful_calls. Uses a single UPDATE with arithmetic to avoid
    a read-modify-write race.
    """
    with _conn() as conn:
        # Only update avg_latency_ms for successful calls. Failed/timed-out
        # calls carry artificially high latencies (e.g. full timeout duration)
        # that inflate the displayed average by orders of magnitude.
        if success:
            conn.execute(
                """
                UPDATE agents
                SET total_calls      = total_calls + 1,
                    avg_latency_ms   = (avg_latency_ms * successful_calls + %s) / (successful_calls + 1),
                    successful_calls = successful_calls + 1
                WHERE agent_id = %s
                """,
                (latency_ms, agent_id),
            )
        else:
            conn.execute(
                "UPDATE agents SET total_calls = total_calls + 1 WHERE agent_id = %s",
                (agent_id,),
            )
        row = conn.execute(
            "SELECT price_per_call_cents, price_per_call_usd, call_latency_ring FROM agents WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
        if row is None:
            return
        if not success:
            return
        effective_price_cents = price_cents
        if effective_price_cents is None:
            effective_price_cents = (
                _to_non_negative_int(row["price_per_call_cents"], default=0)
                if row["price_per_call_cents"] is not None
                else _price_usd_to_cents(row["price_per_call_usd"])
            )
        conn.execute(
            "UPDATE agents SET call_latency_ring = %s WHERE agent_id = %s",
            (
                append_call_ring_sample(
                    row["call_latency_ring"],
                    latency_ms=latency_ms,
                    price_cents=int(effective_price_cents),
                ),
                agent_id,
            ),
        )


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


def get_agents(
    tag: str | None = None,
    include_internal: bool = False,
    include_banned: bool = False,
    include_unapproved: bool = True,
    include_sunset: bool = False,
    model_provider: str | None = None,
) -> list:
    """
    Return all agent listings, optionally filtered by tag or model_provider.
    Tag matching uses exact JSON-array membership to avoid substring false-positives.

    'sunset' (review_status) is excluded by default — owner-self-retracted
    listings should not show up in catalog enumeration, health checks, or
    /health agent_count. Admin paths that need to see them must pass
    include_sunset=True (mirrors include_banned for the row-level status).
    """
    normalized_provider = str(model_provider or "").strip().lower() or None
    with _conn() as conn:
        where_clauses: list[str] = []
        params: list[Any] = []
        if not include_internal:
            where_clauses.append("internal_only = 0")
        if not include_banned:
            where_clauses.append("status NOT IN ('banned', 'suspended')")
        if not include_unapproved:
            # See get_agent(): 'probation' is visible alongside 'approved'.
            where_clauses.append("review_status IN ('approved', 'probation')")
        if not include_sunset:
            where_clauses.append("review_status != 'sunset'")
        if tag:
            where_clauses.append("tags LIKE %s")
            params.append(f'%"{tag}"%')
        if normalized_provider:
            where_clauses.append("model_provider = %s")
            params.append(normalized_provider)
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        rows = conn.execute(
            f"SELECT * FROM agents {where_sql} ORDER BY created_at",
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_agent_status(
    agent_id: str, status: str, reason: str | None = None
) -> dict | None:
    """Update an agent's status to ``active``, ``suspended``, or ``banned``."""
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"active", "suspended", "banned"}:
        raise ValueError("status must be one of: active, suspended, banned.")
    with _conn() as conn:
        if normalized_status == "suspended" and reason is not None:
            conn.execute(
                "UPDATE agents SET status = %s, suspension_reason = %s WHERE agent_id = %s",
                (normalized_status, str(reason)[:500], agent_id),
            )
        else:
            conn.execute(
                "UPDATE agents SET status = %s WHERE agent_id = %s",
                (normalized_status, agent_id),
            )
    return get_agent(agent_id, include_unapproved=True)


def list_pending_review_agents(limit: int = 200) -> list[dict]:
    """Return agents in ``pending_review`` status awaiting admin moderation."""
    capped = min(max(1, int(limit)), 1000)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM agents
            WHERE review_status = 'pending_review'
            ORDER BY created_at DESC, agent_id DESC
            LIMIT %s
            """,
            (capped,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def set_agent_review_decision(
    agent_id: str,
    *,
    decision: str,
    reviewed_by: str,
    note: str | None = None,
    reviewed_at: str | None = None,
) -> dict | None:
    """Record an admin approve/reject decision for an agent under review.

    ``decision`` must be ``"approve"`` or ``"reject"``. Approved agents move
    to ``active``; rejected agents move to ``suspended``.
    """
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approve", "reject"}:
        raise ValueError("decision must be one of: approve, reject.")
    target_status = "approved" if normalized_decision == "approve" else "rejected"
    normalized_reviewed_by = str(reviewed_by or "").strip()
    if not normalized_reviewed_by:
        raise ValueError("reviewed_by must be a non-empty string.")
    normalized_note = str(note or "").strip() or None
    normalized_reviewed_at = str(
        reviewed_at or datetime.now(timezone.utc).isoformat()
    ).strip()
    with _conn() as conn:
        updated = conn.execute(
            """
            UPDATE agents
            SET review_status = %s,
                review_note = %s,
                reviewed_at = %s,
                reviewed_by = %s
            WHERE agent_id = %s
            """,
            (
                target_status,
                normalized_note,
                normalized_reviewed_at,
                normalized_reviewed_by,
                agent_id,
            ),
        ).rowcount
    if updated == 0:
        return None
    return get_agent(agent_id, include_unapproved=True)


def sunset_agent(
    agent_id: str,
    *,
    actor_owner_id: str,
    reason: str | None = None,
) -> dict | None:
    """Mark an agent as sunset (review_status='sunset'). Caller must already
    be authorized (owner or admin) — this function only does the DB write.

    Sunset is the user-facing "removed" state: the call hot path returns
    HTTP 410 ``agent.sunset`` and list/search filters hide the row. Reversible
    via ``reactivate_agent``. Receipts and signed history remain intact.
    """
    normalized_actor = str(actor_owner_id or "").strip()
    if not normalized_actor:
        raise ValueError("actor_owner_id must be a non-empty string.")
    note = (str(reason).strip()[:500] if reason else None) or "self-retracted"
    now_iso = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        updated = conn.execute(
            """
            UPDATE agents
            SET review_status = 'sunset',
                review_note   = %s,
                reviewed_at   = %s,
                reviewed_by   = %s
            WHERE agent_id = %s
            """,
            (note, now_iso, normalized_actor, agent_id),
        ).rowcount
    if updated == 0:
        return None
    return get_agent(agent_id, include_unapproved=True)


def reactivate_agent(
    agent_id: str,
    *,
    actor_owner_id: str,
) -> dict | None:
    """Reverse a prior sunset, restoring ``review_status='approved'``. Caller
    must already be authorized — owners can reverse their own ``sunset``,
    admins can reverse anyone's. Banned agents must use ``set_agent_status``."""
    normalized_actor = str(actor_owner_id or "").strip()
    if not normalized_actor:
        raise ValueError("actor_owner_id must be a non-empty string.")
    now_iso = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        updated = conn.execute(
            """
            UPDATE agents
            SET review_status = 'approved',
                review_note   = NULL,
                reviewed_at   = %s,
                reviewed_by   = %s
            WHERE agent_id = %s AND review_status = 'sunset'
            """,
            (now_iso, normalized_actor, agent_id),
        ).rowcount
    if updated == 0:
        return None
    return get_agent(agent_id, include_unapproved=True)


def delete_agent(agent_id: str) -> bool:
    """Hard-delete an agent row. Admin-only — caller must enforce auth.

    Receipts (``jobs.output_signature``, ``jobs.output_signed_by_did``) reference
    ``agent_id`` as a denormalized string with no FK cascade, so historical
    job rows and signed receipts continue to verify after deletion. The agent's
    DID document at ``/agents/{id}/did.json`` will start returning 404 — that
    is the intended provenance signal that the agent has been retired.

    In-flight jobs must be cancelled and refunded BEFORE calling this; this
    function only performs the DELETE.
    """
    with _conn() as conn:
        updated = conn.execute(
            "DELETE FROM agents WHERE agent_id = %s",
            (agent_id,),
        ).rowcount
    return bool(updated)


def is_agent_sunset(agent: dict | None) -> bool:
    """Unified sunset check used by the call hot path and list filters.

    True when the agent is in either:
    - the legacy hardcoded ``SUNSET_DEPRECATED_AGENT_IDS`` frozenset
      (kept for one release while built-in entries are migrated to DB rows), or
    - ``review_status == 'sunset'`` (owner self-retract or admin sunset).

    Importing the frozenset lazily avoids a circular import at module load:
    ``server.builtin_agents.constants`` imports from this package.
    """
    if not isinstance(agent, dict):
        return False
    review_status = str(agent.get("review_status") or "").strip().lower()
    if review_status == "sunset":
        return True
    agent_id = str(agent.get("agent_id") or "").strip()
    if not agent_id:
        return False
    try:
        from server.builtin_agents.constants import SUNSET_DEPRECATED_AGENT_IDS
    except ImportError:
        return False
    return agent_id in SUNSET_DEPRECATED_AGENT_IDS


_TERMINAL_DISPUTE_STATUSES = ("resolved", "final")


def graduate_probation_listings() -> list[str]:
    """Promote eligible probation agents to ``review_status='approved'``.

    Returns the list of agent_ids graduated this run. Idempotent — safe to
    call from a sweeper. Reads thresholds from :mod:`core.feature_flags`.

    INVARIANTS:
      - Only transitions ``probation`` → ``approved``. Never touches
        ``rejected``, ``pending_review``, or already-``approved`` rows.
      - Writes ``reviewed_by='system'`` and a ``review_note`` describing
        the gates passed, so the audit trail makes the source obvious.

    A row graduates when it clears ALL of:
      1. ``successful_calls >= AZTEA_PROBATION_MIN_SUCCESSES``
      2. ``successful_calls / total_calls >= AZTEA_PROBATION_MIN_SUCCESS_RATE``
      3. average ``job_quality_ratings.rating >= AZTEA_PROBATION_MIN_QUALITY``
      4. zero open disputes (status not in resolved/final)
      5. ``now - created_at >= AZTEA_PROBATION_MIN_AGE_HOURS``
    """
    min_successes = _feature_flags.probation_min_successes()
    min_success_rate = _feature_flags.probation_min_success_rate()
    min_quality = _feature_flags.probation_min_quality()
    min_age_seconds = _feature_flags.probation_min_age_hours() * 3600.0
    now = datetime.now(timezone.utc)

    with _conn() as conn:
        candidates = conn.execute(
            """
            SELECT agent_id, total_calls, successful_calls, created_at
            FROM agents
            WHERE review_status = 'probation'
            """
        ).fetchall()

    graduated: list[str] = []
    for row in candidates:
        agent_id = row["agent_id"]
        try:
            total_calls = int(row["total_calls"] or 0)
            successful = int(row["successful_calls"] or 0)
            if successful < min_successes:
                continue
            success_rate = (successful / total_calls) if total_calls > 0 else 0.0
            if success_rate < min_success_rate:
                continue

            created_at = _parse_iso_to_utc(row["created_at"])
            if created_at is None:
                # Defensive: a row without created_at predates the column or
                # is corrupt; skip rather than graduate blind.
                continue
            if (now - created_at).total_seconds() < min_age_seconds:
                continue

            with _conn() as conn:
                open_disputes = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM disputes d
                    JOIN jobs j ON j.job_id = d.job_id
                    WHERE j.agent_id = %s
                      AND d.status NOT IN ('resolved', 'final')
                    """,
                    (agent_id,),
                ).fetchone()
                if (open_disputes or {}).get("n", 0) > 0:
                    continue

                quality_row = conn.execute(
                    """
                    SELECT AVG(rating) AS avg_rating, COUNT(*) AS rating_count
                    FROM job_quality_ratings
                    WHERE agent_id = %s
                    """,
                    (agent_id,),
                ).fetchone()
            avg_rating = (quality_row or {}).get("avg_rating")
            rating_count = int((quality_row or {}).get("rating_count") or 0)
            # Require at least one rating; without ratings the quality gate
            # is unverifiable. Publishers can still see calls succeed; they
            # just need at least one caller to rate before graduation.
            if rating_count == 0 or avg_rating is None:
                continue
            if float(avg_rating) < min_quality:
                continue

            note = (
                f"auto-graduated: {successful}/{total_calls} successes, "
                f"avg rating {float(avg_rating):.2f} over {rating_count}, "
                f"no open disputes."
            )
            set_agent_review_decision(
                agent_id,
                decision="approve",
                reviewed_by="system",
                note=note,
            )
            graduated.append(agent_id)
        except Exception:
            # One bad row must not abort the batch. The exception path is
            # rare (DB lock contention, malformed timestamp) and the next
            # sweep will retry.
            _logger.exception("auto-graduation failed for %s", agent_id)
            continue

    return graduated


def _parse_iso_to_utc(value: Any) -> datetime | None:
    """Best-effort ISO timestamp → UTC-aware datetime, mirroring helpers used
    elsewhere in this package. Returns None on parse failure rather than
    raising — callers treat None as "skip this row"."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).strip().replace("Z", "+00:00")
        # SQLite often stores `YYYY-MM-DD HH:MM:SS` without a T separator.
        if "T" not in text and " " in text and len(text) >= 19:
            text = text.replace(" ", "T", 1)
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def set_agent_verified(agent_id: str, verified: bool) -> dict | None:
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET verified = %s WHERE agent_id = %s",
            (1 if verified else 0, agent_id),
        )
    return get_agent(agent_id, include_unapproved=True)


def set_agent_endpoint_health(
    agent_id: str,
    *,
    endpoint_health_status: str,
    endpoint_consecutive_failures: int,
    endpoint_last_checked_at: str,
    endpoint_last_error: str | None,
) -> dict | None:
    """Record the result of an endpoint health probe on an agent.

    ``endpoint_health_status`` must be ``"unknown"``, ``"healthy"``, or
    ``"degraded"``. Called by the background sweeper after each probe cycle.
    """
    normalized_status = str(endpoint_health_status or "").strip().lower()
    if normalized_status not in {"unknown", "healthy", "degraded"}:
        raise ValueError(
            "endpoint_health_status must be one of: unknown, healthy, degraded."
        )
    normalized_failures = _to_non_negative_int(endpoint_consecutive_failures, default=0)
    checked_at = str(endpoint_last_checked_at or "").strip()
    if not checked_at:
        raise ValueError("endpoint_last_checked_at must be a non-empty ISO timestamp.")
    normalized_error = str(endpoint_last_error or "").strip() or None
    with _conn() as conn:
        conn.execute(
            """
            UPDATE agents
            SET endpoint_health_status = %s,
                endpoint_consecutive_failures = %s,
                endpoint_last_checked_at = %s,
                endpoint_last_error = %s
            WHERE agent_id = %s
            """,
            (
                normalized_status,
                normalized_failures,
                checked_at,
                normalized_error,
                agent_id,
            ),
        )
        if normalized_failures >= HEALTH_SUSPENSION_THRESHOLD:
            result = conn.execute(
                """
                UPDATE agents
                SET status = 'suspended', suspension_reason = 'health_check'
                WHERE agent_id = %s AND status = 'active'
                """,
                (agent_id,),
            )
            if result.rowcount:
                _logger.warning(
                    "agent %s auto-suspended after %d consecutive endpoint failures",
                    agent_id,
                    normalized_failures,
                )
        elif normalized_failures == 0:
            result = conn.execute(
                """
                UPDATE agents
                SET status = 'active', suspension_reason = NULL
                WHERE agent_id = %s AND status = 'suspended' AND suspension_reason = 'health_check'
                """,
                (agent_id,),
            )
            if result.rowcount:
                _logger.info(
                    "agent %s reinstated after endpoint health recovery",
                    agent_id,
                )
    return get_agent(agent_id, include_unapproved=True)


def set_agent_output_examples(
    agent_id: str, output_examples: list[dict] | None
) -> dict | None:
    """Replace the full set of work examples for an agent. Pass None to clear."""
    normalized_examples: str | None
    if output_examples is None:
        normalized_examples = None
    elif isinstance(output_examples, list):
        normalized_examples = json.dumps(
            [item for item in output_examples if isinstance(item, dict)]
        )
    else:
        raise ValueError("output_examples must be a list of objects or null.")
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET output_examples = %s WHERE agent_id = %s",
            (normalized_examples, agent_id),
        )
    return get_agent(agent_id, include_unapproved=True)


def append_agent_output_example(
    agent_id: str, example: dict, *, max_examples: int = 20
) -> dict | None:
    """Append one work example to an agent's ring buffer, trimming to ``max_examples``."""
    if not isinstance(example, dict):
        raise ValueError("example must be an object.")
    capped = min(max(1, int(max_examples)), 100)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT output_examples FROM agents WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
        if row is None:
            return None
        raw_examples = row["output_examples"]
        parsed_examples: list[dict] = []
        if raw_examples:
            try:
                loaded = json.loads(raw_examples)
                if isinstance(loaded, list):
                    parsed_examples = [
                        item for item in loaded if isinstance(item, dict)
                    ]
            except (TypeError, json.JSONDecodeError):
                parsed_examples = []
        next_examples = [example] + parsed_examples
        if len(next_examples) > capped:
            next_examples = next_examples[:capped]
        conn.execute(
            "UPDATE agents SET output_examples = %s WHERE agent_id = %s",
            (json.dumps(next_examples), agent_id),
        )
    return get_agent(agent_id, include_unapproved=True)


def touch_agent_decay(agent_id: str, at_iso: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET last_decay_at = %s WHERE agent_id = %s",
            (str(at_iso or _CANONICAL_CREATED_AT), agent_id),
        )


def set_agent_decay_multiplier(agent_id: str, multiplier: float, at_iso: str) -> None:
    parsed = _to_non_negative_float(multiplier, default=1.0)
    parsed = max(0.0, min(1.0, parsed))
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET trust_decay_multiplier = %s, last_decay_at = %s WHERE agent_id = %s",
            (parsed, str(at_iso or _CANONICAL_CREATED_AT), agent_id),
        )


def get_agent(agent_id: str, *, include_unapproved: bool = True) -> dict | None:
    """Return a single agent listing by ID, or None if not found.

    'probation' is treated as visible alongside 'approved': probationary
    listings are live and callable; auto_hire ranking + price gates are the
    soft brake. Filtering them out here would amount to silent rejection.
    """
    where_sql = "agent_id = %s"
    if not include_unapproved:
        where_sql += " AND review_status IN ('approved', 'probation')"
    with _conn() as conn:
        row = conn.execute(
            f"SELECT * FROM agents WHERE {where_sql}",
            (agent_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_agents_by_ids(
    agent_ids: list[str], *, include_unapproved: bool = True
) -> dict[str, dict]:
    """Bulk lookup: return ``{agent_id: row_dict}`` for the requested IDs.

    One DB round-trip instead of N. Callers that iterate a batch of
    requests against the registry (e.g. ``POST /jobs/batch``) used to
    do ``registry.get_agent`` per row, which was the dominant cost
    behind the 60s gateway timeout on batches >25 jobs observed in the
    2026-05-17 test report. This helper closes that bottleneck.

    Missing agents are simply absent from the returned dict — callers
    decide how to handle the gap (the batch path emits a per-row 404).
    """
    if not agent_ids:
        return {}
    # Deduplicate to keep the placeholder list short when callers
    # accidentally request the same agent twice.
    unique_ids = list({str(a) for a in agent_ids if a})
    if not unique_ids:
        return {}
    placeholders = ",".join(["%s"] * len(unique_ids))
    where_sql = f"agent_id IN ({placeholders})"
    if not include_unapproved:
        where_sql += " AND review_status IN ('approved', 'probation')"
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM agents WHERE {where_sql}",
            tuple(unique_ids),
        ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        row_dict = _row_to_dict(row)
        out[str(row_dict.get("agent_id"))] = row_dict
    return out


def get_agents_by_owner(owner_id: str) -> list:
    """Return all agents owned by the given owner_id."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agents WHERE owner_id = %s ORDER BY created_at",
            (owner_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_owner_agents(owner_id: str) -> int:
    """Return the number of non-deleted agents owned by this user."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE owner_id = %s AND (status IS NULL OR status != 'deleted')",
            (owner_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def _validate_price_for_update(price_per_call_usd: Any) -> float:
    try:
        price = float(price_per_call_usd)
    except (TypeError, ValueError):
        raise ValueError("price_per_call_usd must be a number.")
    if not math.isfinite(price) or price < 0:
        raise ValueError("price_per_call_usd must be a non-negative finite number.")
    return price


def _build_agent_update_columns(
    *,
    name: str | None,
    description: str | None,
    tags: list | None,
    price_per_call_usd: float | None,
    pii_safe: bool | None,
    outputs_not_stored: bool | None,
    audit_logged: bool | None,
    region_locked: str | None,
    cacheable: bool | None,
    payout_curve: dict | str | None,
    clear_payout_curve: bool,
) -> dict[str, object]:
    """Pure: turn update_agent kwargs into the {column: value} write set."""
    updates: dict[str, object] = {}
    if name is not None:
        n = str(name).strip()
        if not n:
            raise ValueError("name must not be empty.")
        updates["name"] = n
    if description is not None:
        updates["description"] = str(description).strip()
    if tags is not None:
        updates["tags"] = json.dumps(_parse_tags(tags))
    if price_per_call_usd is not None:
        updates["price_per_call_usd"] = _validate_price_for_update(price_per_call_usd)
    if pii_safe is not None:
        updates["pii_safe"] = 1 if pii_safe else 0
    if outputs_not_stored is not None:
        updates["outputs_not_stored"] = 1 if outputs_not_stored else 0
    if audit_logged is not None:
        updates["audit_logged"] = 1 if audit_logged else 0
    if region_locked is not None:
        updates["region_locked"] = str(region_locked).strip().lower() or None
    if cacheable is not None:
        updates["cacheable"] = 1 if cacheable else 0
    if clear_payout_curve:
        updates["payout_curve"] = None
    elif payout_curve is not None:
        from core import payout_curve as _pc
        try:
            parsed_curve = _pc.parse_curve(payout_curve)
        except ValueError as exc:
            raise ValueError(str(exc))
        updates["payout_curve"] = _pc.curve_to_json(parsed_curve)
    return updates


def update_agent(
    agent_id: str,
    owner_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    tags: list | None = None,
    price_per_call_usd: float | None = None,
    pii_safe: bool | None = None,
    outputs_not_stored: bool | None = None,
    audit_logged: bool | None = None,
    region_locked: str | None = None,
    payout_curve: dict | str | None = None,
    clear_payout_curve: bool = False,
    cacheable: bool | None = None,
) -> dict | None:
    """
    Update mutable fields on an agent. Only the owner can call this.
    Returns the updated agent dict, or None if not found / wrong owner.
    """
    updates = _build_agent_update_columns(
        name=name, description=description, tags=tags,
        price_per_call_usd=price_per_call_usd,
        pii_safe=pii_safe, outputs_not_stored=outputs_not_stored,
        audit_logged=audit_logged, region_locked=region_locked,
        cacheable=cacheable, payout_curve=payout_curve,
        clear_payout_curve=clear_payout_curve,
    )
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = %s AND owner_id = %s",
            (agent_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        if not updates:
            return _row_to_dict(row)
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [agent_id, owner_id]
        conn.execute(
            f"UPDATE agents SET {set_clause} WHERE agent_id = %s AND owner_id = %s",
            values,
        )
        updated_row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = %s", (agent_id,)
        ).fetchone()
    return _row_to_dict(updated_row) if updated_row else None


def delist_agent(agent_id: str, owner_id: str) -> bool:
    """
    Soft-delete an agent by setting status='deleted'. Only the owner can do this.
    Returns True if found and updated, False if not found or wrong owner.
    """
    with _conn() as conn:
        result = conn.execute(
            "UPDATE agents SET status = 'deleted' WHERE agent_id = %s AND owner_id = %s AND status != 'deleted'",
            (agent_id, owner_id),
        )
    return result.rowcount > 0


def update_agent_health(agent_id: str, status: str, checked_at: str) -> None:
    """Record the result of a health check probe."""
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET last_health_status = %s, last_health_check_at = %s WHERE agent_id = %s",
            (status, checked_at, agent_id),
        )


def mark_agent_published_public(
    agent_id: str,
    listing_id: str | None,
    published_at: str,
    *,
    owner_id: str,
) -> bool:
    """Record that this agent has been syndicated to aztea.ai's public registry.

    Called from the /registry/agents/{id}/publish route after the hosted API
    confirms the listing was accepted. Idempotent — re-publish updates the
    timestamp and listing_id. Local-only deployments never call this.

    The ``owner_id`` parameter is required. The UPDATE only succeeds when
    the agent row's ``owner_id`` matches; this is a defence-in-depth check
    so that even if the calling route forgets to pre-authorise ownership,
    the data layer refuses to mark a foreign user's agent as published.
    Returns True iff a row was updated.
    """
    if not owner_id:
        raise ValueError("owner_id is required for mark_agent_published_public")
    with _conn() as conn:
        result = conn.execute(
            "UPDATE agents SET published_to_public_at = %s, "
            "published_to_public_listing_id = %s "
            "WHERE agent_id = %s AND owner_id = %s",
            (published_at, listing_id, agent_id, owner_id),
        )
    return getattr(result, "rowcount", 0) > 0


def agent_exists_by_name(name: str) -> bool:
    """Return True if any agent with this name is already registered."""
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM agents WHERE name = %s", (name,)).fetchone()
    return row is not None


def sync_agent_embedding(agent_id: str) -> bool:
    """
    Re-embed one agent if its current source_text has changed.
    This is the helper future update paths should call after mutating
    name/description/tags/input_schema.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Agent '{agent_id}' not found.")

        agent = _row_to_dict(row)
        source_text = _embedding_source_from_agent(agent)
        changed = _upsert_agent_embedding_row(conn, agent_id, source_text)
    if changed:
        _invalidate_embeddings_cache()
    return changed


def backfill_missing_embeddings(limit: int | None = None) -> dict[str, int]:
    """
    Embed existing agents that do not yet have rows in agent_embeddings.
    Safe to run repeatedly (idempotent).
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1 when provided.")

    with _conn() as conn:
        query = """
            SELECT a.*
            FROM agents AS a
            LEFT JOIN agent_embeddings AS e ON e.agent_id = a.agent_id
            WHERE e.agent_id IS NULL
            ORDER BY a.created_at, a.agent_id
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT %s"
            params = (limit,)
        rows = conn.execute(query, params).fetchall()

    to_embed: list[tuple[str, str, list[float]]] = []
    for row in rows:
        agent = _row_to_dict(row)
        source_text = _embedding_source_from_agent(agent)
        to_embed.append(
            (
                str(agent["agent_id"]),
                source_text,
                embeddings.embed_text(source_text),
            )
        )

    embedded = 0
    if to_embed:
        with _conn() as conn:
            for agent_id, source_text, vector in to_embed:
                if _upsert_agent_embedding_row(
                    conn,
                    agent_id=agent_id,
                    source_text=source_text,
                    embedding_vector=vector,
                ):
                    embedded += 1
    if embedded > 0:
        _invalidate_embeddings_cache()

    return {"scanned": len(rows), "embedded": embedded}


def _normalize_trust_score(value: float | int | None) -> float:
    trust = _to_non_negative_float(value, default=0.0)
    if trust > 1.0:
        trust = trust / _TRUST_PERCENT_SCALE
    return max(0.0, min(1.0, trust))


def _normalize_min_trust(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError("min_trust must be between 0.0 and 1.0.")
    if not math.isfinite(parsed):
        raise ValueError("min_trust must be between 0.0 and 1.0.")
    if parsed < 0.0:
        raise ValueError("min_trust must be between 0.0 and 1.0.")
    if parsed > 1.0 and parsed <= _TRUST_PERCENT_SCALE:
        parsed = parsed / _TRUST_PERCENT_SCALE
    if parsed < 0.0 or parsed > 1.0:
        raise ValueError("min_trust must be between 0.0 and 1.0.")
    return parsed


def _price_usd_to_cents(value: float | int | str | None) -> int:
    try:
        amount = Decimal(str(value))
    except Exception:
        return 0
    if amount < 0:
        return 0
    cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if amount > 0 and cents == 0:
        return 1
    return cents


def _required_input_fields_set(required_input_fields: list[str] | None) -> set[str]:
    if not required_input_fields:
        return set()
    fields: set[str] = set()
    for field in required_input_fields:
        value = str(field).strip()
        if not value:
            raise ValueError("required_input_fields entries must be non-empty strings.")
        fields.add(value)
    return fields


def _input_schema_field_names(schema: dict) -> set[str]:
    if not isinstance(schema, dict):
        return set()

    properties = schema.get("properties")
    if isinstance(properties, dict):
        return {str(name) for name in properties.keys()}

    fields = schema.get("fields")
    if isinstance(fields, list):
        names: set[str] = set()
        for field in fields:
            if isinstance(field, dict):
                candidate = str(field.get("name") or "").strip()
                if candidate:
                    names.add(candidate)
        return names
    return set()


def _input_schema_caller_trust_min(schema: dict) -> float | None:
    if not isinstance(schema, dict):
        return None
    candidate = schema.get("min_caller_trust")
    if candidate is None and isinstance(schema.get("metadata"), dict):
        candidate = schema["metadata"].get("min_caller_trust")
    if candidate is None:
        return None
    try:
        value = float(candidate)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if value > 1.0 and value <= 100.0:
        value = value / 100.0
    if value < 0.0 or value > 1.0:
        return None
    return value


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[a-z0-9-]+", query.lower())
    return [term for term in terms if term not in _QUERY_STOP_WORDS]


def _example_search_text(output_examples: Any) -> str:
    if not isinstance(output_examples, list):
        return ""
    parts: list[str] = []
    for example in output_examples[:3]:
        if not isinstance(example, dict):
            continue
        for key in ("input", "output"):
            value = example.get(key)
            if value is None:
                continue
            parts.append(json.dumps(value, sort_keys=True, ensure_ascii=True)[:800])
    return " ".join(parts)


def _lexical_overlap_score(query_terms: list[str], haystack: str) -> float:
    if not query_terms:
        return 0.0
    lowered = haystack.lower()
    hit_count = sum(1 for term in query_terms if term in lowered)
    return hit_count / len(query_terms)


def _lexical_match_score(query: str, agent: dict, supported_fields: set[str]) -> float:
    query_terms = _query_terms(query)
    if not query_terms:
        return 0.0

    name_text = str(agent.get("name") or "")
    desc_text = str(agent.get("description") or "")
    tag_text = " ".join(_parse_tags(agent.get("tags")))
    field_text = " ".join(sorted(supported_fields))
    example_text = _example_search_text(agent.get("output_examples"))

    name_score = _lexical_overlap_score(query_terms, name_text)
    desc_score = _lexical_overlap_score(query_terms, desc_text)
    tag_score = _lexical_overlap_score(query_terms, tag_text)
    field_score = _lexical_overlap_score(query_terms, field_text)
    example_score = _lexical_overlap_score(query_terms, example_text)

    lowered_query = query.lower()
    name_lower = name_text.lower()
    desc_lower = desc_text.lower()
    tag_lower = tag_text.lower()
    example_lower = example_text.lower()
    # The slug is the canonical identifier callers type — treat exact slug
    # token matches as the strongest relevance signal short of a full phrase
    # match. Without this, "regex matching" surfaced secret-scanner above
    # regex-tester because both had similar lexical scores and trust dominated
    # the tie-break (eval finding 2026-05-09).
    slug_tokens = {tok for tok in _query_terms(name_text) if tok}
    slug_token_hit = any(term in slug_tokens for term in query_terms)

    phrase_bonus = 0.0
    if lowered_query in name_lower:
        phrase_bonus += 0.25
    elif lowered_query in desc_lower:
        phrase_bonus += 0.18
    elif lowered_query in example_lower:
        phrase_bonus += 0.12

    if query_terms and all(term in name_lower for term in query_terms):
        phrase_bonus += 0.12
    if query_terms and any(term in tag_lower for term in query_terms):
        phrase_bonus += 0.08
    # Strong bonus for an exact slug-token match — outweighs the trust gap
    # that previously let high-trust adjacent agents win an unambiguous query.
    if slug_token_hit:
        phrase_bonus += 0.20

    score = (
        0.38 * name_score
        + 0.26 * desc_score
        + 0.18 * tag_score
        + 0.08 * field_score
        + 0.10 * example_score
        + phrase_bonus
    )
    return max(0.0, min(1.0, score))


# Routing overlay populated at startup by ``server.application`` from the
# built-in spec definitions. Kept in this module so ``search_agents`` can
# read curated match/block keyword lists without violating the core →
# server one-way import rule. Overlay is keyed by agent_id.
_ROUTING_OVERLAY_MATCH: dict[str, list[str]] = {}
_ROUTING_OVERLAY_BLOCK: dict[str, list[str]] = {}
_ROUTING_OVERLAY_INSTALLED: bool = False


def set_routing_overlay(
    match_keywords: dict[str, list[str]] | None,
    block_keywords: dict[str, list[str]] | None,
) -> None:
    """Install the per-agent routing keyword overlay used by search ranking.

    Called from the FastAPI lifespan AND lazily on first search read so a
    worker that missed the lifespan path (race, exception, lazy import
    quirk) still routes correctly. Idempotent: calling multiple times
    just refreshes the maps.
    """
    global _ROUTING_OVERLAY_MATCH, _ROUTING_OVERLAY_BLOCK, _ROUTING_OVERLAY_INSTALLED
    _ROUTING_OVERLAY_MATCH = {
        str(k): [str(v).strip().lower() for v in (vs or []) if str(v).strip()]
        for k, vs in (match_keywords or {}).items()
        if k
    }
    _ROUTING_OVERLAY_BLOCK = {
        str(k): [str(v).strip().lower() for v in (vs or []) if str(v).strip()]
        for k, vs in (block_keywords or {}).items()
        if k
    }
    _ROUTING_OVERLAY_INSTALLED = True


def _ensure_routing_overlay_loaded() -> None:
    """Self-heal the routing overlay if a worker missed the lifespan path.

    The 2026-05-09 prod verification showed only 1 of 3 uvicorn workers
    populated the overlay through the lifespan hook — the other two had
    empty maps and silently degraded ranking, which made the eval's hit
    rate look randomly variable. This function imports the built-in spec
    catalog at first-read time and installs the overlay if it's still
    empty. Cheap (one dict comprehension over ~10 specs); safe (set
    exactly once per process — the global flag short-circuits subsequent
    calls); robust (no dependency on lifespan ordering).
    """
    global _ROUTING_OVERLAY_INSTALLED
    if _ROUTING_OVERLAY_INSTALLED:
        return
    try:
        # Late import to keep the core → server one-way rule intact at
        # module-load time. server.builtin_agents.specs has no
        # back-import to core.registry, so this resolves cleanly.
        from server.builtin_agents.specs import builtin_agent_specs

        specs = builtin_agent_specs()
        set_routing_overlay(
            match_keywords={
                str(spec.get("agent_id") or ""): list(spec.get("match_keywords") or [])
                for spec in specs
                if spec.get("match_keywords")
            },
            block_keywords={
                str(spec.get("agent_id") or ""): list(spec.get("block_keywords") or [])
                for spec in specs
                if spec.get("block_keywords")
            },
        )
    except Exception:  # noqa: BLE001 — search must never crash on overlay load
        # Silent: keep the overlay flag False so a future call retries
        # rather than caching the failure. Search degrades to lex+sim+
        # trust+price without keyword bonus, which is still functional.
        _ROUTING_OVERLAY_INSTALLED = False


def _agent_match_keywords(agent: dict) -> list[str]:
    """Curated routing vocabulary for an agent.

    Pulls from (a) the agent row's ``match_keywords`` column when present
    (e.g. registered via the SDK with explicit keywords), (b) the routing
    overlay populated from built-in specs at startup. Caught in the
    2026-05-07 eval where bucket-B jargon queries (SBOM, IMDS, ReDoS,
    log4shell, prototype pollution) collapsed to cve_lookup_agent because
    no agent uniquely owned those terms.
    """
    raw = agent.get("match_keywords")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = [raw]
    if isinstance(raw, list) and raw:
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    _ensure_routing_overlay_loaded()
    agent_id = str(agent.get("agent_id") or "").strip()
    return _ROUTING_OVERLAY_MATCH.get(agent_id, [])


def _agent_block_keywords(agent: dict) -> list[str]:
    raw = agent.get("block_keywords")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = [raw]
    if isinstance(raw, list) and raw:
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    _ensure_routing_overlay_loaded()
    agent_id = str(agent.get("agent_id") or "").strip()
    return _ROUTING_OVERLAY_BLOCK.get(agent_id, [])


# Per-cohort term/agent-token sets. All sets are module-level so they're
# never recreated per call.
_SECURITY_TERMS = frozenset({
    "security", "vulnerability", "vulnerabilities", "cve", "cves",
    "secret", "secrets", "credential", "credentials", "password",
    "passwords", "hardcoded", "npm", "package", "dependency",
    "dependencies", "audit",
})
_SECRET_QUERY_TERMS = frozenset({
    "secret", "secrets", "credential", "credentials",
    "password", "passwords", "hardcoded",
})
_VULN_QUERY_TERMS = frozenset({
    "vulnerability", "vulnerabilities", "audit", "dependency",
    "dependencies", "package", "npm",
})
_VULN_AGENT_TOKENS = (
    "dependency", "dependencies", "audit", "package", "npm", "license",
)
_REVIEW_TERMS = frozenset(
    {"review", "reviewer", "diff", "patch", "bugs", "bug", "correctness"}
)
_BROWSER_TERMS = frozenset(
    {"browser", "screenshot", "screenshots", "playwright", "render", "homepage"}
)
_VISUAL_COMPARE_TERMS = frozenset(
    {"compare", "diff", "difference", "regression", "baseline", "before", "after"}
)
_IMAGE_TERMS = frozenset(
    {"image", "generate", "generation", "dall", "replicate", "picture"}
)
# "render" lives ONLY in web-render terms — placing it under image_terms
# inflated visual_regression on "render this webpage" since its description
# contains "image". Web rendering and image generation share zero overlap
# in this catalog.
_WEB_RENDER_TERMS = frozenset({
    "render", "renders", "rendered", "webpage", "web-page",
    "site", "url", "scrape", "crawl",
})
_FINANCE_TERMS = frozenset(
    {"edgar", "10-k", "10q", "10-q", "sec", "filing", "revenue"}
)
_RED_TEAM_TERMS = frozenset(
    {"red", "redteam", "red-teamer", "adversarial", "jailbreak", "prompt"}
)
_SBOM_TERMS = frozenset({"sbom", "license", "licenses", "open", "source"})
_EXECUTION_TERMS = frozenset(
    {"run", "execute", "python", "sandbox", "disk", "write", "filesystem", "jwt", "decode"}
)
_PAGE_SCREENSHOT_TERMS = frozenset({"screenshot", "screenshots", "homepage"})
_BROWSER_AGENT_TOKENS = ("browser", "playwright", "headless", "chromium")
_VR_AGENT_TOKENS = ("visual regression", "pixel-level diff")

_INTENT_BONUS_MIN = -0.35
_INTENT_BONUS_MAX = 0.70


def _curated_keyword_bonus(agent: dict, lowered_query: str) -> float:
    """Pure: curated match/block keyword bonuses — the strongest discovery signal."""
    bonus = 0.0
    match_kws = _agent_match_keywords(agent)
    if match_kws:
        kw_hits = sum(1 for kw in match_kws if kw in lowered_query)
        if kw_hits:
            bonus += min(0.60, kw_hits * 0.20)
    block_kws = _agent_block_keywords(agent)
    if block_kws:
        block_hits = sum(1 for kw in block_kws if kw in lowered_query)
        if block_hits:
            bonus -= min(0.50, block_hits * 0.25)
    return bonus


def _security_cohort_bonus(terms_set: set[str], combined: str) -> float:
    """Pure: security/CVE/secret/dependency cohort bonuses."""
    if not (_SECURITY_TERMS & terms_set):
        return 0.0
    bonus = 0.0
    if _SECRET_QUERY_TERMS & terms_set:
        if any(t in combined for t in ("secret", "credential", "password", "token")):
            bonus += 0.40
        elif any(t in combined for t in ("cve", "nvd", "osv")):
            bonus -= 0.20
    if {"cve", "cves"} & terms_set and any(
        t in combined for t in ("cve", "nvd", "osv")
    ):
        bonus += 0.30
    if _VULN_QUERY_TERMS & terms_set and any(t in combined for t in _VULN_AGENT_TOKENS):
        bonus += 0.25
    return bonus


def _review_cohort_bonus(terms_set: set[str], combined: str) -> float:
    """Pure: code-review cohort; mild penalty when query routes at lint/typecheck instead."""
    if not (_REVIEW_TERMS & terms_set):
        return 0.0
    bonus = 0.0
    if any(t in combined for t in
           ("code review", "review", "diff", "correctness", "maintainability")):
        bonus += 0.20
    if any(t in combined for t in ("linter", "ruff", "eslint", "type checker", "mypy")):
        bonus -= 0.05
    return bonus


def _browser_cohort_bonus(terms_set: set[str], combined: str) -> float:
    """Pure: browser/web-render cohort. Page-screenshot or render intents must beat VR."""
    if not (_BROWSER_TERMS & terms_set or _WEB_RENDER_TERMS & terms_set):
        return 0.0
    visual_compare = bool(_VISUAL_COMPARE_TERMS & terms_set)
    wants_page_screenshot = bool(_PAGE_SCREENSHOT_TERMS & terms_set and not visual_compare)
    wants_web_render = bool(_WEB_RENDER_TERMS & terms_set and not visual_compare)
    if (wants_page_screenshot or wants_web_render) and any(
        t in combined for t in _BROWSER_AGENT_TOKENS
    ):
        return 0.65  # Strong page-fetch signal: must dominate VR's "image" lexical hit.
    if (wants_page_screenshot or wants_web_render) and any(
        t in combined for t in _VR_AGENT_TOKENS
    ):
        return -0.35  # Same intent class routed at the wrong agent.
    if any(t in combined for t in ("browser", "playwright", "screenshot", "headless")):
        return 0.35
    if "secret" in combined or "code review" in combined:
        return -0.20
    return 0.0


def _visual_compare_cohort_bonus(terms_set: set[str], combined: str) -> float:
    """Pure: pixel-diff / visual-regression cohort."""
    if not (_VISUAL_COMPARE_TERMS & terms_set):
        return 0.0
    if any(t in combined for t in
           ("visual regression", "pixel-level diff", "compare two screenshots")):
        return 0.45
    if "browser" in combined:
        return -0.10
    return 0.0


def _other_cohort_bonuses(terms_set: set[str], combined: str) -> float:
    """Pure: image / finance / red-team / SBOM / execution cohort bonuses."""
    bonus = 0.0
    if _IMAGE_TERMS & terms_set:
        if any(t in combined for t in ("image", "generation", "replicate", "gpt-image")):
            bonus += 0.35
        elif any(t in combined for t in ("arxiv", "code review", "secret")):
            bonus -= 0.20
    if _FINANCE_TERMS & terms_set and any(
        t in combined for t in ("edgar", "sec", "10-k", "financial")
    ):
        bonus += 0.35
    if _RED_TEAM_TERMS & terms_set and any(
        t in combined for t in ("red team", "adversarial", "jailbreak")
    ):
        bonus += 0.35
    if _SBOM_TERMS & terms_set and any(
        t in combined for t in ("dependency", "license", "audit", "package")
    ):
        bonus += 0.25
    if _EXECUTION_TERMS & terms_set:
        if {"jwt", "decode"} & terms_set and any(
            t in combined for t in ("python", "execute", "sandbox")
        ):
            bonus += 0.35
        if {"disk", "write", "filesystem"} & terms_set and any(
            t in combined for t in ("python", "sandbox", "execute", "code")
        ):
            bonus += 0.30
    return bonus


def _intent_match_bonus(query: str, agent: dict) -> float:
    """Pure: cohort-aware bonus on top of lexical/semantic search.

    Why: each cohort encodes "if the query is about X, this kind of agent
    is the right answer" — bonuses are clamped so a single cohort can't
    monopolise the blended score.
    """
    terms = _query_terms(query)
    if not terms:
        return 0.0
    name = str(agent.get("name") or "").lower()
    description = str(agent.get("description") or "").lower()
    tags = {str(tag).strip().lower() for tag in _parse_tags(agent.get("tags"))}
    combined = " ".join([name, description, " ".join(sorted(tags))])
    lowered_query = str(query or "").lower()
    terms_set = set(terms)
    bonus = (
        _curated_keyword_bonus(agent, lowered_query)
        + _security_cohort_bonus(terms_set, combined)
        + _review_cohort_bonus(terms_set, combined)
        + _browser_cohort_bonus(terms_set, combined)
        + _visual_compare_cohort_bonus(terms_set, combined)
        + _other_cohort_bonuses(terms_set, combined)
    )
    return max(_INTENT_BONUS_MIN, min(_INTENT_BONUS_MAX, bonus))


def _price_query_mode(query: str) -> str | None:
    terms = set(_query_terms(query))
    # Bare "price"/"cost" are too ambiguous to trigger price-intent ranking on
    # their own (e.g. "apple stock price" is a stock lookup, not a request for
    # the cheapest agent). Require an explicit cheap/low/expensive/highest
    # intent term — "lowest price" and "cheapest cost" still work because
    # those qualifiers carry the intent.
    if not (
        {"cheap", "cheapest", "low", "lowest", "expensive", "highest", "costliest"}
        & terms
    ):
        return None
    if {"expensive", "highest", "costliest"} & terms:
        return "most_expensive"
    if {"cheap", "cheapest", "low", "lowest"} & terms:
        return "cheapest"
    return None


def _matched_phrase(query: str, haystack: str) -> str | None:
    terms = _query_terms(query)
    if not terms:
        return None

    lowered = haystack.lower()
    for width in (3, 2):
        if len(terms) < width:
            continue
        for idx in range(0, len(terms) - width + 1):
            phrase = " ".join(terms[idx : idx + width])
            if phrase in lowered:
                return phrase

    for term in terms:
        if len(term) >= 4 and term in lowered:
            return term
    return None


def _match_reasons(
    agent: dict,
    query: str,
    trust: float,
    required_fields: set[str],
    supported_fields: set[str],
    caller_trust: float | None,
    caller_trust_min: float | None,
) -> list[str]:
    reasons: list[str] = []
    haystack = " ".join(
        [
            str(agent.get("name") or ""),
            str(agent.get("description") or ""),
            " ".join(_parse_tags(agent.get("tags"))),
        ]
    )
    phrase = _matched_phrase(query, haystack)
    if phrase:
        reasons.append(f"matched '{phrase}' in description")
    example_phrase = _matched_phrase(
        query, _example_search_text(agent.get("output_examples"))
    )
    if example_phrase and example_phrase != phrase:
        reasons.append(f"matched '{example_phrase}' in work examples")
    if required_fields:
        if len(required_fields) == 1:
            field = sorted(required_fields)[0]
            reasons.append(f"supports {field} input field")
        else:
            ordered = ", ".join(sorted(supported_fields))
            reasons.append(f"supports input fields: {ordered}")
    reasons.append(f"trust {trust:.2f}")
    if caller_trust is not None and caller_trust_min is not None:
        reasons.append(
            f"caller trust {caller_trust:.2f} meets minimum {caller_trust_min:.2f}"
        )
    return reasons


def _llm_rerank_candidates(
    query: str,
    candidates: list[dict],
) -> list[dict]:
    """Optional LLM re-rank stage. Default no-op until the catalog grows.

    Activation policy (when AZTEA_SEARCH_LLM_RERANK=1):
      * Skip when there's a clear winner — top score >= top2 + 0.15.
      * Skip when there's clearly nothing — top score below content floor.
      * Otherwise: send query + top-N (name, description, category) to a
        small fast model via core.llm.run_with_fallback (Groq llama-3.1-8b
        first in the default chain), with a 500ms timeout, and let it
        re-order or signal "none of these match" → empty list.

    Stub for now: returns the input unchanged. Filling in the body is a
    one-function-touch when the catalog crosses ~30 agents and the
    deterministic ranker starts losing to ambiguous-intent queries. The
    seam is here so that change does not require restructuring search.
    """
    return candidates


_SEARCH_AGENT_KINDS = frozenset({"aztea_built", "community_skill", "self_hosted"})

# Price-mode blend weights (cheapest / most_expensive) — sum to 1.0; price
# dominates because the caller's intent is "I want the cheapest agent".
_PRICE_MODE_PRICE_WEIGHT = 0.62
_PRICE_MODE_LEXICAL_WEIGHT = 0.16
_PRICE_MODE_SEMANTIC_WEIGHT = 0.08
_PRICE_MODE_TRUST_WEIGHT = 0.09
_PRICE_MODE_INTENT_WEIGHT = 0.05


def _validate_search_inputs(
    query: str, limit: int, max_price_cents: int | None,
) -> str:
    """Pure: trim + expand the query and reject malformed numeric bounds."""
    normalized_query = _expand_search_query(str(query or "").strip())
    if not normalized_query:
        raise ValueError("query must be a non-empty string.")
    if limit < 1:
        raise ValueError("limit must be >= 1.")
    if max_price_cents is not None and max_price_cents < 0:
        raise ValueError("max_price_cents must be >= 0 when provided.")
    return normalized_query


def _normalize_search_filters(
    *, model_provider: str | None, kind: str | None,
    region_locked: str | None, min_trust: float,
    caller_trust: float | None, required_input_fields: list[str] | None,
) -> dict[str, Any]:
    """Pure: shape every search filter into its canonical form."""
    normalized_kind = str(kind or "").strip().lower() or None
    if normalized_kind and normalized_kind not in _SEARCH_AGENT_KINDS:
        normalized_kind = None
    return {
        "model_provider": str(model_provider or "").strip().lower() or None,
        "kind": normalized_kind,
        "region_locked": str(region_locked or "").strip().lower() or None,
        "trust_floor": _normalize_min_trust(min_trust),
        "caller_trust": (
            _normalize_min_trust(caller_trust) if caller_trust is not None else None
        ),
        "required_fields": _required_input_fields_set(required_input_fields),
    }


def _agent_matches_filters(
    agent: dict, filters: dict[str, Any], *,
    pii_safe: bool | None, outputs_not_stored: bool | None,
    audit_logged: bool | None, max_price_cents: int | None,
) -> tuple[bool, int, set[str], float | None]:
    """Pure: True when ``agent`` clears every static filter; returns price/fields/trust-min for reuse."""
    if filters["model_provider"] and agent.get("model_provider") != filters["model_provider"]:
        return False, 0, set(), None
    if filters["kind"] and agent.get("kind") != filters["kind"]:
        return False, 0, set(), None
    if pii_safe is not None and bool(agent.get("pii_safe")) != pii_safe:
        return False, 0, set(), None
    if outputs_not_stored is not None and bool(agent.get("outputs_not_stored")) != outputs_not_stored:
        return False, 0, set(), None
    if audit_logged is not None and bool(agent.get("audit_logged")) != audit_logged:
        return False, 0, set(), None
    if (
        filters["region_locked"]
        and str(agent.get("region_locked") or "").strip().lower() != filters["region_locked"]
    ):
        return False, 0, set(), None
    price_cents = _price_usd_to_cents(agent.get("price_per_call_usd"))
    if max_price_cents is not None and price_cents > max_price_cents:
        return False, 0, set(), None
    schema = _parse_input_schema(agent.get("input_schema"))
    supported_fields = _input_schema_field_names(schema)
    caller_trust_min = _input_schema_caller_trust_min(schema)
    if filters["required_fields"] and not filters["required_fields"].issubset(supported_fields):
        return False, 0, set(), None
    if (
        filters["caller_trust"] is not None
        and caller_trust_min is not None
        and filters["caller_trust"] < caller_trust_min
    ):
        return False, 0, set(), None
    if _normalize_trust_score(agent.get("trust_score")) < filters["trust_floor"]:
        return False, 0, set(), None
    return True, price_cents, supported_fields, caller_trust_min


def _semantic_similarity_for(
    agent_id: str, agent: dict,
    query_vector: np.ndarray | None,
    vectors_by_agent: dict[str, np.ndarray],
    missing_embeddings: list[tuple[str, str, list[float]]],
) -> float:
    """Pure-ish: cosine sim against query.

    1.6.7 fix: when an agent's embedding is missing from the cache, this
    function used to call ``embeddings.embed_text()`` synchronously per
    agent — that's ~1s of sentence-transformers inference each. With 37
    agents in the catalog and a cold cache (post-deploy or after a new
    agent registers), a single search took 37+ seconds and triggered the
    MCP client's emergency-fallback path. Now: if the embedding is
    missing, skip semantic similarity for that agent (return 0.0) and
    queue the backfill so the next search is fast. Lexical scoring
    still runs, so the agent isn't dropped from results — it just
    doesn't get the semantic-bonus on this query.
    """
    if query_vector is None:
        return 0.0
    vector = vectors_by_agent.get(agent_id)
    if vector is None:
        # Queue the embedding for background persistence, but do not
        # block the search loop on inference. The next search will
        # pick up the cached vector via _load_embeddings_for_agents.
        try:
            source_text = _embedding_source_from_agent(agent)
            missing_embeddings.append((agent_id, source_text, None))
        except Exception:  # noqa: BLE001 — embedding-source bugs must not 500 the search
            pass
        return 0.0
    similarity = float(embeddings.cosine(query_vector, vector))
    return max(0.0, min(1.0, similarity))


def _build_candidates(
    agents: list[dict], normalized_query: str, filters: dict[str, Any], *,
    pii_safe: bool | None, outputs_not_stored: bool | None,
    audit_logged: bool | None, max_price_cents: int | None,
    embeddings_enabled: bool, query_vector: np.ndarray | None,
    vectors_by_agent: dict[str, np.ndarray],
    missing_embeddings: list[tuple[str, str, list[float]]],
) -> list[dict]:
    """Side-effect: filter agents and score each as a search candidate."""
    candidates: list[dict] = []
    for agent in agents:
        agent_id = str(agent.get("agent_id") or "").strip()
        if not agent_id:
            continue
        ok, price_cents, supported_fields, caller_trust_min = _agent_matches_filters(
            agent, filters,
            pii_safe=pii_safe, outputs_not_stored=outputs_not_stored,
            audit_logged=audit_logged, max_price_cents=max_price_cents,
        )
        if not ok:
            continue
        semantic_similarity = (
            _semantic_similarity_for(
                agent_id, agent, query_vector, vectors_by_agent, missing_embeddings,
            ) if embeddings_enabled else 0.0
        )
        candidates.append({
            "agent": agent,
            "similarity": semantic_similarity,
            "lexical_score": _lexical_match_score(normalized_query, agent, supported_fields),
            "intent_bonus": _intent_match_bonus(normalized_query, agent),
            "trust": _normalize_trust_score(agent.get("trust_score")),
            "price_cents": price_cents,
            "supported_fields": supported_fields,
            "caller_trust_min": caller_trust_min,
        })
    return candidates


def _persist_missing_embeddings(
    missing_embeddings: list[tuple[str, str, list[float] | None]],
) -> None:
    """Side-effect: persist newly-computed embeddings; invalidate cache on any change.

    1.6.7 fix: entries with ``vector_list is None`` mean the search loop
    skipped inference (would have blocked ~1s per agent). Spawn a daemon
    thread to compute + persist those out-of-band so the next search
    sees a warm cache. Pre-computed entries (vector_list not None) are
    written inline as before — fast and safe.
    """
    if not missing_embeddings:
        return
    precomputed = [
        (aid, src, vec) for aid, src, vec in missing_embeddings if vec is not None
    ]
    deferred = [
        (aid, src) for aid, src, vec in missing_embeddings if vec is None
    ]

    if precomputed:
        with _conn() as conn:
            changed = any(
                _upsert_agent_embedding_row(
                    conn, agent_id=aid,
                    source_text=src, embedding_vector=vec,
                )
                for aid, src, vec in precomputed
            )
        if changed:
            _invalidate_embeddings_cache()

    if deferred:
        # Daemon thread — must not block the search response. The next
        # search picks up the cached vectors via _load_embeddings_for_agents.
        import threading as _threading
        _threading.Thread(
            target=_backfill_embeddings_async,
            args=(deferred,),
            daemon=True,
            name="aztea-embedding-backfill",
        ).start()


def _backfill_embeddings_async(deferred: list[tuple[str, str]]) -> None:
    """Side-effect: compute + persist embeddings for ``deferred`` agents.

    Runs in a daemon thread spawned by ``_persist_missing_embeddings``
    so a cold-cache search doesn't block on per-agent inference.
    Failures are logged but never raised — backfill is best-effort.
    """
    try:
        computed: list[tuple[str, str, list[float]]] = []
        for agent_id, source_text in deferred:
            try:
                vector_list = embeddings.embed_text(source_text)
            except Exception:  # noqa: BLE001 — inference can fail for many reasons
                _logger.warning(
                    "embedding backfill: embed_text failed for %s", agent_id
                )
                continue
            computed.append((agent_id, source_text, vector_list))
        if not computed:
            return
        with _conn() as conn:
            changed = any(
                _upsert_agent_embedding_row(
                    conn, agent_id=aid,
                    source_text=src, embedding_vector=vec,
                )
                for aid, src, vec in computed
            )
        if changed:
            _invalidate_embeddings_cache()
    except Exception:  # noqa: BLE001 — daemon thread must never crash the runtime
        _logger.exception("embedding backfill: unexpected failure")


def _compute_inverse_price(
    candidate: dict, min_price: int, max_price: int,
) -> float:
    """Pure: 1.0 when only one price exists, otherwise normalised inverse in [0, 1]."""
    if max_price == min_price:
        return 1.0
    normalized_price = (candidate["price_cents"] - min_price) / (max_price - min_price)
    return 1.0 - normalized_price


def _blend_score(
    candidate: dict, *, inverse_price: float, price_query_mode: str | None,
    embeddings_enabled: bool,
) -> float:
    """Pure: combine lexical/semantic/trust/price/intent into a single ranked score."""
    if price_query_mode is not None:
        price_intent_score = (
            1.0 - inverse_price if price_query_mode == "most_expensive" else inverse_price
        )
        semantic_component = candidate["similarity"] if embeddings_enabled else 0.0
        return (
            _PRICE_MODE_PRICE_WEIGHT * price_intent_score
            + _PRICE_MODE_LEXICAL_WEIGHT * candidate["lexical_score"]
            + _PRICE_MODE_SEMANTIC_WEIGHT * semantic_component
            + _PRICE_MODE_TRUST_WEIGHT * candidate["trust"]
            + _PRICE_MODE_INTENT_WEIGHT * max(0.0, min(1.0, candidate["intent_bonus"]))
        )
    if embeddings_enabled:
        return (
            LEXICAL_SCORE_WEIGHT * candidate["lexical_score"]
            + SEMANTIC_SCORE_WEIGHT * candidate["similarity"]
            + TRUST_SCORE_WEIGHT_HYBRID * candidate["trust"]
            + INVERSE_PRICE_WEIGHT_HYBRID * inverse_price
            + candidate["intent_bonus"]
        )
    # Embeddings disabled: lexical match becomes the primary routing signal so
    # trust/price don't dominate weak text search.
    remaining_weight = 1.0 - LEXICAL_SCORE_WEIGHT
    total_remaining = TRUST_SCORE_WEIGHT_HYBRID + INVERSE_PRICE_WEIGHT_HYBRID
    return (
        LEXICAL_SCORE_WEIGHT * candidate["lexical_score"]
        + (remaining_weight * (TRUST_SCORE_WEIGHT_HYBRID / total_remaining)) * candidate["trust"]
        + (remaining_weight * (INVERSE_PRICE_WEIGHT_HYBRID / total_remaining)) * inverse_price
        + candidate["intent_bonus"]
    )


def _annotate_blended_scores(
    candidates: list[dict], *, normalized_query: str,
    price_query_mode: str | None, embeddings_enabled: bool,
    required_fields: set[str], normalized_caller_trust: float | None,
) -> None:
    """Side-effect (mutating ``candidates``): write ``blended_score`` and ``match_reasons``."""
    price_values = [c["price_cents"] for c in candidates]
    min_price, max_price = min(price_values), max(price_values)
    for candidate in candidates:
        inverse_price = _compute_inverse_price(candidate, min_price, max_price)
        candidate["blended_score"] = _blend_score(
            candidate, inverse_price=inverse_price,
            price_query_mode=price_query_mode, embeddings_enabled=embeddings_enabled,
        )
        candidate["match_reasons"] = _match_reasons(
            candidate["agent"], normalized_query, candidate["trust"],
            required_fields, candidate["supported_fields"],
            normalized_caller_trust, candidate["caller_trust_min"],
        )
        if price_query_mode == "cheapest":
            candidate["match_reasons"].append("ranked by lowest caller price")
        elif price_query_mode == "most_expensive":
            candidate["match_reasons"].append("ranked by highest caller price")


def _sort_candidates(
    candidates: list[dict], price_query_mode: str | None,
) -> list[dict]:
    """Pure: order candidates by mode-specific tie-breakers.

    Why: in 'most_expensive' mode, every agent at the top price must be
    a tied #1 — sorting by price first prevents lexical noise from
    arbitrarily selecting one of three tied agents at $0.03.
    """
    if price_query_mode == "most_expensive":
        return sorted(candidates, key=lambda i: (
            -i["price_cents"], i["blended_score"], i["similarity"], i["trust"],
        ))
    if price_query_mode == "cheapest":
        return sorted(candidates, key=lambda i: (
            i["price_cents"], -i["blended_score"], -i["similarity"], -i["trust"],
        ))
    return sorted(candidates, key=lambda i: (
        i["blended_score"], i["similarity"], i["trust"], -i["price_cents"],
    ), reverse=True)


def _is_off_catalog_query(normalized_query: str) -> bool:
    """Pure: True when the query unambiguously asks for a capability we don't have."""
    query_token_set = set(_query_terms(normalized_query))
    if not query_token_set:
        return False
    for _description, predicate in _OFF_CATALOG_PATTERNS:
        try:
            if predicate(query_token_set):
                return True
        except Exception:  # noqa: BLE001 — predicates must never crash search
            continue
    return False


def _maybe_llm_rerank(
    ranked: list[dict], normalized_query: str, price_query_mode: str | None,
) -> list[dict]:
    """Side-effect: optional LLM re-rank stage; gated off by default and never blocks search."""
    if not (ranked and price_query_mode is None
            and _feature_flags.search_llm_rerank_enabled()):
        return ranked
    try:
        return _llm_rerank_candidates(normalized_query, ranked)
    except Exception:  # noqa: BLE001 — re-rank must never block search
        return ranked


def _apply_relevance_floor(ranked: list[dict]) -> list[dict]:
    """Pure-ish: drop low-relevance candidates so callers don't get mediocre matches.

    Why: returning weak distractors is worse than returning empty —
    empty signals "use a different query" while distractors create
    false confidence in low-relevance results.
    """
    if not ranked:
        return ranked
    top_score = ranked[0]["blended_score"]
    floor = _feature_flags.search_relevance_floor()
    keep = _feature_flags.search_keep_floor()
    band = _feature_flags.search_dropoff_band()
    if top_score < floor:
        return []
    return [
        item for item in ranked
        if item["blended_score"] >= keep or item["blended_score"] >= top_score - band
    ]


def search_agents(
    query: str,
    limit: int = 10,
    min_trust: float = 0.0,
    max_price_cents: int | None = None,
    required_input_fields: list[str] | None = None,
    caller_trust: float | None = None,
    include_unapproved: bool = True,
    model_provider: str | None = None,
    kind: str | None = None,
    pii_safe: bool | None = None,
    outputs_not_stored: bool | None = None,
    audit_logged: bool | None = None,
    region_locked: str | None = None,
) -> list[dict]:
    """Side-effect: search the agent registry by keyword + embedding similarity with optional filters.

    Why: blended scoring (lexical + semantic + trust + price + intent
    bonuses) plus a relevance floor prevents weak distractors from
    appearing as #1 when nothing in the catalog actually fits the query.
    """
    normalized_query = _validate_search_inputs(query, limit, max_price_cents)
    filters = _normalize_search_filters(
        model_provider=model_provider, kind=kind, region_locked=region_locked,
        min_trust=min_trust, caller_trust=caller_trust,
        required_input_fields=required_input_fields,
    )
    price_query_mode = _price_query_mode(normalized_query)
    embeddings_enabled = not _feature_flags.DISABLE_EMBEDDINGS
    query_vector: np.ndarray | None = (
        np.asarray(embeddings.embed_text(normalized_query), dtype=np.float32)
        if embeddings_enabled else None
    )
    agents = get_agents_with_reputation(include_unapproved=include_unapproved)
    vectors_by_agent = _load_embeddings_for_agents({
        str(a.get("agent_id") or "").strip()
        for a in agents
        if str(a.get("agent_id") or "").strip()
    })
    missing_embeddings: list[tuple[str, str, list[float]]] = []
    candidates = _build_candidates(
        agents, normalized_query, filters,
        pii_safe=pii_safe, outputs_not_stored=outputs_not_stored,
        audit_logged=audit_logged, max_price_cents=max_price_cents,
        embeddings_enabled=embeddings_enabled, query_vector=query_vector,
        vectors_by_agent=vectors_by_agent, missing_embeddings=missing_embeddings,
    )
    _persist_missing_embeddings(missing_embeddings)
    if not candidates:
        return []
    _annotate_blended_scores(
        candidates, normalized_query=normalized_query,
        price_query_mode=price_query_mode, embeddings_enabled=embeddings_enabled,
        required_fields=filters["required_fields"],
        normalized_caller_trust=filters["caller_trust"],
    )
    ranked = _sort_candidates(candidates, price_query_mode)
    if _is_off_catalog_query(normalized_query):
        return []
    ranked = _apply_relevance_floor(_maybe_llm_rerank(ranked, normalized_query, price_query_mode))
    return [
        {
            "agent": item["agent"],
            "similarity": round(item["similarity"], 6),
            "lexical_score": round(item["lexical_score"], 6),
            "trust": round(item["trust"], 6),
            "blended_score": round(item["blended_score"], 6),
            "match_reasons": item["match_reasons"],
        }
        for item in ranked[:limit]
    ]


def get_agents_with_reputation(
    tag: str | None = None,
    *,
    include_unapproved: bool = True,
    model_provider: str | None = None,
) -> list:
    """Return listings enriched with trust/reputation fields for ranking."""
    from core import reputation

    return reputation.enrich_agent_records(
        get_agents(
            tag=tag,
            include_unapproved=include_unapproved,
            model_provider=model_provider,
        )
    )


def get_agent_with_reputation(
    agent_id: str, *, include_unapproved: bool = True
) -> dict | None:
    """Return one enriched listing by agent_id, or None if missing."""
    from core import reputation

    agent = get_agent(agent_id, include_unapproved=include_unapproved)
    return reputation.enrich_agent_record(agent) if agent else None
