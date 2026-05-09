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
    (
        "research papers / academic literature",
        lambda toks: bool(
            {"papers", "paper", "arxiv", "academic", "preprint", "preprints", "research"}
            & toks
        ),
    ),
    (
        "TypeScript / mypy type-checking",
        lambda toks: ({"type", "typecheck", "type-check", "typecheck", "tsc", "mypy"} & toks)
        and ("typescript" in toks or "ts" in toks or "python" in toks),
    ),
    (
        "image generation",
        lambda toks: bool(
            {"dall", "midjourney", "stable", "diffusion", "generator"} & toks
        )
        and ("image" in toks or "picture" in toks),
    ),
    (
        "endpoint latency / load testing",
        lambda toks: ({"endpoint", "endpoints", "latency", "load"} & toks)
        and ({"fast", "slow", "test", "perf", "performance", "p99", "p95"} & toks),
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
    """
    Insert a new agent listing. Returns the agent_id.
    Pass agent_id explicitly for deterministic IDs (e.g. self-registration).
    By default this also writes an embedding row in the same request.
    Raises _db.IntegrityError if agent_id already exists.
    """
    aid = agent_id or str(uuid.uuid4())
    normalized_owner_id = (owner_id or f"agent:{aid}").strip()
    if not normalized_owner_id:
        raise ValueError("owner_id must be a non-empty string.")

    is_internal = internal_only or str(endpoint_url or "").strip().startswith(
        "internal://"
    )

    # Run all pure scalar validation before touching the DB.
    _scalars = _validate_agent_scalar_params(
        price_per_call_usd,
        endpoint_health_status,
        status,
        review_status,
        is_internal,
        pricing_model,
        pricing_config,
    )
    _scalars.raise_on_err()
    price = _scalars.value["price"]
    normalized_health_status = _scalars.value["normalized_health_status"]
    normalized_status = _scalars.value["normalized_status"]
    normalized_review_status = _scalars.value["normalized_review_status"]
    normalized_pricing_model = _scalars.value["normalized_pricing_model"]
    pricing_config_json = _scalars.value["pricing_config_json"]

    created_at = datetime.now(timezone.utc).isoformat()
    normalized_tags = _parse_tags(tags)
    normalized_schema = _parse_input_schema(input_schema)
    normalized_output_schema = _parse_output_schema(output_schema)
    schema_json = json.dumps(normalized_schema, sort_keys=True)
    output_schema_json = json.dumps(normalized_output_schema, sort_keys=True)
    tags_json = json.dumps(normalized_tags)
    normalized_healthcheck_url = str(healthcheck_url or "").strip() or None
    normalized_verifier_url = str(output_verifier_url or "").strip() or None
    if isinstance(output_examples, list):
        normalized_examples: str | None = (
            json.dumps([ex for ex in output_examples if isinstance(ex, dict)]) or None
        )
    else:
        normalized_examples = None
    normalized_verified = 1 if verified else 0
    normalized_pii_safe = 1 if pii_safe else 0
    normalized_outputs_not_stored = 1 if outputs_not_stored else 0
    normalized_audit_logged = 1 if audit_logged else 0
    normalized_region_locked = str(region_locked or "").strip().lower() or None
    normalized_cacheable = None if cacheable is None else (1 if cacheable else 0)
    from core import payout_curve as _pc

    try:
        parsed_curve = _pc.parse_curve(payout_curve)
    except ValueError as exc:
        raise ValueError(str(exc))
    payout_curve_json = _pc.curve_to_json(parsed_curve)
    normalized_review_note = str(review_note or "").strip() or None
    normalized_reviewed_at = str(reviewed_at or "").strip() or None
    normalized_reviewed_by = str(reviewed_by or "").strip() or None
    normalized_decay_multiplier = _to_non_negative_float(
        trust_decay_multiplier, default=1.0
    )
    if normalized_decay_multiplier <= 0:
        normalized_decay_multiplier = 1.0
    internal_only_int = 1 if internal_only else 0
    source_text = ""
    embedding_vector: list[float] | None = None
    if embed_listing:
        source_text = _build_embedding_source_text(
            name, description, normalized_tags, normalized_schema
        )
        embedding_vector = embeddings.embed_text(source_text)

    # Cryptographic identity. Generated up front so the same row insert
    # carries the DID, public key, and private key. We tolerate failures
    # (missing ``cryptography`` lib in some test envs) by leaving the
    # fields NULL — the agent still registers, just without a signing key.
    agent_did_value: str | None = None
    private_pem_value: str | None = None
    public_pem_value: str | None = None
    signing_keys_created_at: str | None = None
    try:
        from core import crypto as _crypto
        from core.identity import build_agent_did as _build_agent_did

        private_pem_value, public_pem_value = _crypto.generate_signing_keypair()
        agent_did_value = _build_agent_did(aid)
        signing_keys_created_at = created_at
    except Exception:
        _logger.exception("Failed to generate signing keypair for agent %s", aid)

    with _conn() as conn:
        valid_kinds = {"aztea_built", "community_skill", "self_hosted"}
        normalized_kind = str(kind or "self_hosted").strip().lower()
        if normalized_kind not in valid_kinds:
            normalized_kind = "self_hosted"

        conn.execute(
            """
            INSERT INTO agents
                (agent_id, owner_id, name, description, endpoint_url, healthcheck_url,
                 price_per_call_usd, tags, input_schema, output_schema, output_verifier_url,
                 output_examples, verified, endpoint_health_status, endpoint_consecutive_failures,
                 endpoint_last_checked_at, endpoint_last_error,
                 internal_only, status, review_status, review_note, reviewed_at, reviewed_by,
                 trust_decay_multiplier, last_decay_at, created_at,
                 model_provider, model_id, pricing_model, pricing_config, kind,
                 pii_safe, outputs_not_stored, audit_logged, region_locked, payout_curve, cacheable)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, NULL, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                aid,
                normalized_owner_id,
                name,
                description,
                endpoint_url,
                normalized_healthcheck_url,
                price,
                tags_json,
                schema_json,
                output_schema_json,
                normalized_verifier_url,
                normalized_examples,
                normalized_verified,
                normalized_health_status,
                internal_only_int,
                normalized_status,
                normalized_review_status,
                normalized_review_note,
                normalized_reviewed_at,
                normalized_reviewed_by,
                normalized_decay_multiplier,
                created_at,
                created_at,
                str(model_provider).strip().lower() if model_provider else None,
                str(model_id).strip()[:128] if model_id else None,
                normalized_pricing_model,
                pricing_config_json,
                normalized_kind,
                normalized_pii_safe,
                normalized_outputs_not_stored,
                normalized_audit_logged,
                normalized_region_locked,
                payout_curve_json,
                normalized_cacheable,
            ),
        )
        if embed_listing and embedding_vector is not None:
            _upsert_agent_embedding_row(
                conn,
                agent_id=aid,
                source_text=source_text,
                embedding_vector=embedding_vector,
            )
        if agent_did_value is not None and private_pem_value is not None:
            try:
                conn.execute(
                    """
                    UPDATE agents
                    SET did = %s,
                        signing_public_key = %s,
                        signing_private_key = %s,
                        signing_keys_created_at = %s
                    WHERE agent_id = %s
                    """,
                    (
                        agent_did_value,
                        public_pem_value,
                        private_pem_value,
                        signing_keys_created_at,
                        aid,
                    ),
                )
            except _db.OperationalError as exc:
                # Column may not exist on a database that hasn't picked up
                # migration 0015 yet — log and continue. Backfill on the
                # next startup will retry.
                _logger.warning(
                    "Could not persist signing keypair for agent %s (schema not yet migrated?): %s",
                    aid,
                    exc,
                )
    if embed_listing:
        _invalidate_embeddings_cache()

    # Eagerly create the agent's sub-wallet, linked to its owner's wallet.
    # ``owner_id`` here is the *human owner* of the agent (a "user:<id>" string
    # for marketplace agents, or "agent:<id>" for self-owned built-ins). The
    # agent's payout wallet is keyed by ``"agent:<aid>"``.
    try:
        from core import payments as _payments

        parent_wallet_id: str | None = None
        # Only link to a parent if the agent has a *different* owner than itself.
        # A self-owned agent (owner_id == "agent:<aid>") has no human parent.
        if normalized_owner_id and normalized_owner_id != f"agent:{aid}":
            owner_wallet = _payments.get_or_create_wallet(normalized_owner_id)
            parent_wallet_id = owner_wallet["wallet_id"]
        _payments.get_or_create_wallet(
            f"agent:{aid}",
            parent_wallet_id=parent_wallet_id,
            display_label=name[:80] if name else None,
        )
    except Exception:
        # Wallet creation is best-effort at registration time. The job-creation
        # path also calls ``get_or_create_wallet`` and will recover if this
        # eager step failed.
        _logger.exception("failed to eagerly create agent sub-wallet for %s", aid)

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
    model_provider: str | None = None,
) -> list:
    """
    Return all agent listings, optionally filtered by tag or model_provider.
    Tag matching uses exact JSON-array membership to avoid substring false-positives.
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
            where_clauses.append("review_status = 'approved'")
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
    """Return a single agent listing by ID, or None if not found."""
    where_sql = "agent_id = %s"
    if not include_unapproved:
        where_sql += " AND review_status = 'approved'"
    with _conn() as conn:
        row = conn.execute(
            f"SELECT * FROM agents WHERE {where_sql}",
            (agent_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


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
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = %s AND owner_id = %s",
            (agent_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        agent = _row_to_dict(row)

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
            try:
                price = float(price_per_call_usd)
            except (TypeError, ValueError):
                raise ValueError("price_per_call_usd must be a number.")
            if not math.isfinite(price) or price < 0:
                raise ValueError(
                    "price_per_call_usd must be a non-negative finite number."
                )
            updates["price_per_call_usd"] = price
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

        if not updates:
            return agent

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
    phrase_bonus = 0.0
    if lowered_query in name_text.lower():
        phrase_bonus += 0.25
    elif lowered_query in desc_text.lower():
        phrase_bonus += 0.18
    elif lowered_query in example_text.lower():
        phrase_bonus += 0.12

    if query_terms and all(term in name_text.lower() for term in query_terms):
        phrase_bonus += 0.12
    if query_terms and any(term in tag_text.lower() for term in query_terms):
        phrase_bonus += 0.08

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


def set_routing_overlay(
    match_keywords: dict[str, list[str]] | None,
    block_keywords: dict[str, list[str]] | None,
) -> None:
    """Install the per-agent routing keyword overlay used by search ranking.

    Called once at application startup from the FastAPI lifespan. The
    server layer owns the spec definitions; core registers them here so
    the search ranker (which lives in core) can use them without a
    server → core → server import cycle.
    """
    global _ROUTING_OVERLAY_MATCH, _ROUTING_OVERLAY_BLOCK
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
            import json as _json

            raw = _json.loads(raw)
        except Exception:
            raw = [raw]
    if isinstance(raw, list) and raw:
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    agent_id = str(agent.get("agent_id") or "").strip()
    return _ROUTING_OVERLAY_MATCH.get(agent_id, [])


def _agent_block_keywords(agent: dict) -> list[str]:
    raw = agent.get("block_keywords")
    if isinstance(raw, str):
        try:
            import json as _json

            raw = _json.loads(raw)
        except Exception:
            raw = [raw]
    if isinstance(raw, list) and raw:
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    agent_id = str(agent.get("agent_id") or "").strip()
    return _ROUTING_OVERLAY_BLOCK.get(agent_id, [])


def _intent_match_bonus(query: str, agent: dict) -> float:
    terms = _query_terms(query)
    if not terms:
        return 0.0

    name = str(agent.get("name") or "").lower()
    description = str(agent.get("description") or "").lower()
    tags = {str(tag).strip().lower() for tag in _parse_tags(agent.get("tags"))}
    combined = " ".join([name, description, " ".join(sorted(tags))])
    lowered_query = str(query or "").lower()
    bonus = 0.0

    # Curated match_keywords are the single strongest discovery signal —
    # they encode "if the query mentions X, this is the right agent" in
    # plain language. Each hit adds 0.20 (capped at 0.60) to the bonus,
    # which the blended score then weighs alongside lexical+semantic.
    match_kws = _agent_match_keywords(agent)
    if match_kws:
        kw_hits = sum(1 for kw in match_kws if kw in lowered_query)
        if kw_hits:
            bonus += min(0.60, kw_hits * 0.20)

    # block_keywords pull the agent down so it doesn't grab the slot it
    # shouldn't. Example: json_schema_validator must not match
    # "package.json vulnerabilities" — the schema validator has no CVE data.
    block_kws = _agent_block_keywords(agent)
    if block_kws:
        block_hits = sum(1 for kw in block_kws if kw in lowered_query)
        if block_hits:
            bonus -= min(0.50, block_hits * 0.25)

    security_terms = {
        "security",
        "vulnerability",
        "vulnerabilities",
        "cve",
        "cves",
        "secret",
        "secrets",
        "credential",
        "credentials",
        "password",
        "passwords",
        "hardcoded",
        "npm",
        "package",
        "dependency",
        "dependencies",
        "audit",
    }
    review_terms = {"review", "reviewer", "diff", "patch", "bugs", "bug", "correctness"}
    browser_terms = {
        "browser",
        "screenshot",
        "screenshots",
        "playwright",
        "render",
        "homepage",
    }
    visual_compare_terms = {
        "compare",
        "diff",
        "difference",
        "regression",
        "baseline",
        "before",
        "after",
    }
    image_terms = {
        "image",
        "generate",
        "generation",
        "dall",
        "replicate",
        "picture",
    }
    # "render" used to live in image_terms, but the 2026-05-08 eval found that
    # "render this webpage" matched image_terms (visual_regression description
    # contains "image"), inflating its blended score above browser_agent. Web
    # rendering and image generation share zero overlap in this catalog, so
    # bonus the browser path explicitly without overloading the image path.
    web_render_terms = {
        "render",
        "renders",
        "rendered",
        "webpage",
        "web-page",
        "site",
        "url",
        "scrape",
        "crawl",
    }
    finance_terms = {"edgar", "10-k", "10q", "10-q", "sec", "filing", "revenue"}
    red_team_terms = {
        "red",
        "redteam",
        "red-teamer",
        "adversarial",
        "jailbreak",
        "prompt",
    }
    sbom_terms = {"sbom", "license", "licenses", "open", "source"}
    execution_terms = {
        "run",
        "execute",
        "python",
        "sandbox",
        "disk",
        "write",
        "filesystem",
        "jwt",
        "decode",
    }

    if security_terms & set(terms):
        if {
            "secret",
            "secrets",
            "credential",
            "credentials",
            "password",
            "passwords",
            "hardcoded",
        } & set(terms):
            if any(
                token in combined
                for token in ("secret", "credential", "password", "token")
            ):
                bonus += 0.40
            elif any(token in combined for token in ("cve", "nvd", "osv")):
                bonus -= 0.20
        if {"cve", "cves"} & set(terms) and any(
            token in combined for token in ("cve", "nvd", "osv")
        ):
            bonus += 0.30
        if {
            "vulnerability",
            "vulnerabilities",
            "audit",
            "dependency",
            "dependencies",
            "package",
            "npm",
        } & set(terms):
            if any(
                token in combined
                for token in (
                    "dependency",
                    "dependencies",
                    "audit",
                    "package",
                    "npm",
                    "license",
                )
            ):
                bonus += 0.25

    if review_terms & set(terms):
        if any(
            token in combined
            for token in (
                "code review",
                "review",
                "diff",
                "correctness",
                "maintainability",
            )
        ):
            bonus += 0.20
        if any(
            token in combined
            for token in ("linter", "ruff", "eslint", "type checker", "mypy")
        ):
            bonus -= 0.05

    if browser_terms & set(terms) or web_render_terms & set(terms):
        wants_page_screenshot = bool(
            {"screenshot", "screenshots", "homepage"} & set(terms)
            and not (visual_compare_terms & set(terms))
        )
        wants_web_render = bool(
            web_render_terms & set(terms)
            and not (visual_compare_terms & set(terms))
        )
        if (wants_page_screenshot or wants_web_render) and any(
            token in combined for token in ("browser", "playwright", "headless", "chromium")
        ):
            # Strong page-fetch signal: render/scrape/screenshot a real URL.
            # Browser Agent must dominate Visual Regression here regardless of
            # the "image" lexical match VR catches via "compare two images".
            bonus += 0.65
        elif (wants_page_screenshot or wants_web_render) and any(
            token in combined for token in ("visual regression", "pixel-level diff")
        ):
            # Same intent class but routed at the wrong agent — push it down.
            bonus -= 0.35
        elif any(
            token in combined
            for token in ("browser", "playwright", "screenshot", "headless")
        ):
            bonus += 0.35
        elif "secret" in combined or "code review" in combined:
            bonus -= 0.20
    if visual_compare_terms & set(terms):
        if any(
            token in combined
            for token in (
                "visual regression",
                "pixel-level diff",
                "compare two screenshots",
            )
        ):
            bonus += 0.45
        elif "browser" in combined:
            bonus -= 0.10
    if image_terms & set(terms):
        if any(
            token in combined
            for token in ("image", "generation", "replicate", "gpt-image")
        ):
            bonus += 0.35
        elif any(token in combined for token in ("arxiv", "code review", "secret")):
            bonus -= 0.20
    if finance_terms & set(terms):
        if any(token in combined for token in ("edgar", "sec", "10-k", "financial")):
            bonus += 0.35
    if red_team_terms & set(terms):
        if any(token in combined for token in ("red team", "adversarial", "jailbreak")):
            bonus += 0.35
    if sbom_terms & set(terms):
        if any(
            token in combined for token in ("dependency", "license", "audit", "package")
        ):
            bonus += 0.25
    if execution_terms & set(terms):
        if {"jwt", "decode"} & set(terms) and any(
            token in combined for token in ("python", "execute", "sandbox")
        ):
            bonus += 0.35
        if {"disk", "write", "filesystem"} & set(terms) and any(
            token in combined for token in ("python", "sandbox", "execute", "code")
        ):
            bonus += 0.30

    return max(-0.35, min(0.70, bonus))


def _price_query_mode(query: str) -> str | None:
    terms = set(_query_terms(query))
    if not (
        {"cheap", "cheapest", "low", "lowest", "price", "cost", "expensive", "highest"}
        & terms
    ):
        return None
    if {"expensive", "highest", "costliest"} & terms:
        return "most_expensive"
    if {"cheap", "cheapest", "low", "lowest", "price", "cost"} & terms:
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
    """Search the agent registry by keyword + embedding similarity with optional filters.

    Falls back to keyword-only search when no embedding model is available.
    Filters: ``min_trust``, ``max_price_cents``, ``pii_safe``, ``kind``, etc.
    Returns up to ``limit`` agents ranked by combined keyword + semantic score.
    """
    normalized_query = _expand_search_query(str(query or "").strip())
    if not normalized_query:
        raise ValueError("query must be a non-empty string.")
    if limit < 1:
        raise ValueError("limit must be >= 1.")
    if max_price_cents is not None and max_price_cents < 0:
        raise ValueError("max_price_cents must be >= 0 when provided.")
    normalized_model_provider = str(model_provider or "").strip().lower() or None
    valid_kinds = {"aztea_built", "community_skill", "self_hosted"}
    normalized_kind = str(kind or "").strip().lower() or None
    if normalized_kind and normalized_kind not in valid_kinds:
        normalized_kind = None
    normalized_region_locked = str(region_locked or "").strip().lower() or None

    trust_floor = _normalize_min_trust(min_trust)
    normalized_caller_trust = None
    if caller_trust is not None:
        normalized_caller_trust = _normalize_min_trust(caller_trust)
    required_fields = _required_input_fields_set(required_input_fields)
    price_query_mode = _price_query_mode(normalized_query)
    # Skip embedding computation when disabled; the semantic weight is
    # redistributed to trust and price in the blending step below.
    _embeddings_enabled = not _feature_flags.DISABLE_EMBEDDINGS
    query_vector: np.ndarray | None = None
    if _embeddings_enabled:
        query_vector = np.asarray(
            embeddings.embed_text(normalized_query), dtype=np.float32
        )
    agents = get_agents_with_reputation(include_unapproved=include_unapproved)
    vectors_by_agent = _load_embeddings_for_agents(
        {
            str(agent.get("agent_id") or "").strip()
            for agent in agents
            if str(agent.get("agent_id") or "").strip()
        }
    )

    missing_embeddings: list[tuple[str, str, list[float]]] = []
    candidates: list[dict] = []

    for agent in agents:
        agent_id = str(agent.get("agent_id") or "").strip()
        if not agent_id:
            continue

        if (
            normalized_model_provider
            and agent.get("model_provider") != normalized_model_provider
        ):
            continue

        if normalized_kind and agent.get("kind") != normalized_kind:
            continue
        if pii_safe is not None and bool(agent.get("pii_safe")) != pii_safe:
            continue
        if (
            outputs_not_stored is not None
            and bool(agent.get("outputs_not_stored")) != outputs_not_stored
        ):
            continue
        if audit_logged is not None and bool(agent.get("audit_logged")) != audit_logged:
            continue
        if (
            normalized_region_locked
            and str(agent.get("region_locked") or "").strip().lower()
            != normalized_region_locked
        ):
            continue

        price_cents = _price_usd_to_cents(agent.get("price_per_call_usd"))
        if max_price_cents is not None and price_cents > max_price_cents:
            continue

        schema = _parse_input_schema(agent.get("input_schema"))
        supported_fields = _input_schema_field_names(schema)
        caller_trust_min = _input_schema_caller_trust_min(schema)
        if required_fields and not required_fields.issubset(supported_fields):
            continue
        if (
            normalized_caller_trust is not None
            and caller_trust_min is not None
            and normalized_caller_trust < caller_trust_min
        ):
            continue

        trust = _normalize_trust_score(agent.get("trust_score"))
        if trust < trust_floor:
            continue

        semantic_similarity = 0.0
        if _embeddings_enabled and query_vector is not None:
            vector = vectors_by_agent.get(agent_id)
            if vector is None:
                source_text = _embedding_source_from_agent(agent)
                vector_list = embeddings.embed_text(source_text)
                vector = np.asarray(vector_list, dtype=np.float32)
                vectors_by_agent[agent_id] = vector
                missing_embeddings.append((agent_id, source_text, vector_list))
            similarity = float(embeddings.cosine(query_vector, vector))
            semantic_similarity = max(0.0, min(1.0, similarity))
        lexical_score = _lexical_match_score(
            normalized_query,
            agent,
            supported_fields,
        )
        intent_bonus = _intent_match_bonus(normalized_query, agent)
        candidates.append(
            {
                "agent": agent,
                "similarity": semantic_similarity,  # 0.0 when embeddings disabled
                "lexical_score": lexical_score,
                "intent_bonus": intent_bonus,
                "trust": trust,
                "price_cents": price_cents,
                "supported_fields": supported_fields,
                "caller_trust_min": caller_trust_min,
            }
        )

    if missing_embeddings:
        with _conn() as conn:
            changed = False
            for agent_id, source_text, vector_list in missing_embeddings:
                if _upsert_agent_embedding_row(
                    conn,
                    agent_id=agent_id,
                    source_text=source_text,
                    embedding_vector=vector_list,
                ):
                    changed = True
        if changed:
            _invalidate_embeddings_cache()

    if not candidates:
        return []

    price_values = [c["price_cents"] for c in candidates]
    min_price = min(price_values)
    max_price = max(price_values)

    for candidate in candidates:
        if max_price == min_price:
            inverse_price = 1.0
        else:
            normalized_price = (candidate["price_cents"] - min_price) / (
                max_price - min_price
            )
            inverse_price = 1.0 - normalized_price

        price_intent_score = inverse_price
        if price_query_mode == "most_expensive":
            price_intent_score = 1.0 - inverse_price

        if price_query_mode is not None:
            semantic_component = candidate["similarity"] if _embeddings_enabled else 0.0
            blended_score = (
                0.62 * price_intent_score
                + 0.16 * candidate["lexical_score"]
                + 0.08 * semantic_component
                + 0.09 * candidate["trust"]
                + 0.05 * max(0.0, min(1.0, candidate["intent_bonus"]))
            )
        elif _embeddings_enabled:
            blended_score = (
                LEXICAL_SCORE_WEIGHT * candidate["lexical_score"]
                + SEMANTIC_SCORE_WEIGHT * candidate["similarity"]
                + TRUST_SCORE_WEIGHT_HYBRID * candidate["trust"]
                + INVERSE_PRICE_WEIGHT_HYBRID * inverse_price
                + candidate["intent_bonus"]
            )
        else:
            # Embeddings disabled: lexical matching becomes the primary routing
            # signal instead of letting trust/price dominate weak text search.
            remaining_weight = 1.0 - LEXICAL_SCORE_WEIGHT
            total_remaining = TRUST_SCORE_WEIGHT_HYBRID + INVERSE_PRICE_WEIGHT_HYBRID
            blended_score = (
                LEXICAL_SCORE_WEIGHT * candidate["lexical_score"]
                + (remaining_weight * (TRUST_SCORE_WEIGHT_HYBRID / total_remaining))
                * candidate["trust"]
                + (remaining_weight * (INVERSE_PRICE_WEIGHT_HYBRID / total_remaining))
                * inverse_price
                + candidate["intent_bonus"]
            )
        candidate["blended_score"] = blended_score
        candidate["match_reasons"] = _match_reasons(
            candidate["agent"],
            normalized_query,
            candidate["trust"],
            required_fields,
            candidate["supported_fields"],
            normalized_caller_trust,
            candidate["caller_trust_min"],
        )
        if price_query_mode == "cheapest":
            candidate["match_reasons"].append("ranked by lowest caller price")
        elif price_query_mode == "most_expensive":
            candidate["match_reasons"].append("ranked by highest caller price")

    if price_query_mode == "most_expensive":
        # Sort by price first so every agent at the maximum price surfaces as
        # a tied #1 — a single highest-price agent winning on lexical noise
        # was the prior bug ("most expensive" returned only one of three
        # agents at $0.03). Within a price tier, fall back to the existing
        # blended-score tie-break so the most relevant of the tied agents
        # leads the group.
        ranked = sorted(
            candidates,
            key=lambda item: (
                -item["price_cents"],
                item["blended_score"],
                item["similarity"],
                item["trust"],
            ),
        )
    elif price_query_mode == "cheapest":
        ranked = sorted(
            candidates,
            key=lambda item: (
                item["price_cents"],
                -item["blended_score"],
                -item["similarity"],
                -item["trust"],
            ),
        )
    else:
        ranked = sorted(
            candidates,
            key=lambda item: (
                item["blended_score"],
                item["similarity"],
                item["trust"],
                -item["price_cents"],
            ),
            reverse=True,
        )

    # Off-catalog short-circuit: when the query unambiguously asks for a
    # capability we don't have, return empty BEFORE the relevance-floor
    # check so a weak lexical match can't sneak through.
    query_token_set = set(_query_terms(normalized_query))
    if price_query_mode is None and query_token_set:
        for _description, predicate in _OFF_CATALOG_PATTERNS:
            try:
                if predicate(query_token_set):
                    return []
            except Exception:  # noqa: BLE001 — predicates must never crash search
                continue

    # Content-relevance gate (2026-05-09 fix): the existing relevance_floor
    # only checks `blended_score`, which is a weighted sum that includes
    # trust and inverse-price as additive components. A high-trust agent
    # with average price contributes >0.10 to blended_score even when both
    # lexical and semantic overlap with the query are zero — so queries
    # like "tell me a joke" or "cook me dinner" cleared the floor and
    # returned three random code-execution agents in the eval. The fix
    # gates on actual content match: the top candidate must have either a
    # MEANINGFUL lexical match (>= a small floor — NOT just one
    # coincidental common word like "me" matching "use me for X" in an
    # agent description) OR semantic similarity above the content floor.
    # If neither, the catalog has nothing topically relevant and we
    # return empty regardless of how high trust/price boosted the blend.
    #
    # Both thresholds are env-tunable so production can retune without a
    # redeploy if the catalog grows in ways that shift the noise band.
    # Defaults sized against real-world embeddings: sentence-transformers
    # MiniLM cosine sits ~0.10–0.20 for unrelated short queries, so 0.45
    # stays safely above noise; lexical scores from a single coincidental
    # common-word match land near 0.02–0.05, so 0.10 keeps those out
    # while still admitting any real keyword overlap.
    if price_query_mode is None and ranked:
        top = ranked[0]
        _content_floor = _feature_flags.search_content_floor()
        _lex_floor = _feature_flags.search_lexical_content_floor()
        has_content_signal = (
            float(top.get("lexical_score") or 0.0) >= _lex_floor
            or float(top.get("similarity") or 0.0) >= _content_floor
        )
        if not has_content_signal:
            ranked = []

    # Optional LLM re-rank seam (2026-05-09): when the catalog grows past
    # ~30 agents, lexical+embedding can struggle to disambiguate among
    # several semantically-overlapping candidates. The stage below is
    # gated off by default and is a no-op until AZTEA_SEARCH_LLM_RERANK=1
    # is set in the env. The implementation lives in
    # `_llm_rerank_candidates` so this site stays small and the stub can
    # be filled with a real Groq/llama call when needed without touching
    # the surrounding ranking logic.
    if (
        ranked
        and price_query_mode is None
        and _feature_flags.search_llm_rerank_enabled()
    ):
        try:
            ranked = _llm_rerank_candidates(normalized_query, ranked)
        except Exception:  # noqa: BLE001 — re-rank must never block search
            pass

    # Confidence floor: drop low-relevance candidates so callers don't get
    # five mediocre matches when nothing in the catalog is a real fit. The
    # eval flagged "find recent papers", "image generator", "agents that
    # take credit cards" all returning weak unrelated agents because there
    # was no empty-result mode. We still keep at least the top hit if its
    # score crosses the floor; otherwise the response signals "no match" to
    # the caller via an empty list (callers branch on `count == 0`).
    if price_query_mode is None and ranked:
        top_score = ranked[0]["blended_score"]
        # Reload thresholds per-call: env-tunable without redeploy
        # (see AZTEA_SEARCH_* in core/feature_flags.py).
        _floor = _feature_flags.search_relevance_floor()
        _keep = _feature_flags.search_keep_floor()
        _band = _feature_flags.search_dropoff_band()
        if top_score >= _floor:
            ranked = [
                item for item in ranked
                if item["blended_score"] >= _keep
                or item["blended_score"] >= top_score - _band
            ]
        else:
            # No agent matches strongly. Returning weak distractors is worse
            # than returning empty — empty signals "use a different query"
            # while distractors create false confidence in low-relevance
            # results.
            ranked = []

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
