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
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import numpy as np

from core import embeddings

from .core_schema import (
    HEALTH_SUSPENSION_THRESHOLD,
    INVERSE_PRICE_WEIGHT,
    REVIEW_STATUSES,
    SEMANTIC_SIMILARITY_WEIGHT,
    TRUST_SCORE_WEIGHT,
    _CANONICAL_CREATED_AT,
    _QUERY_STOP_WORDS,
    _TRUST_PERCENT_SCALE,
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
    VALID_PRICING_MODELS,
    VariablePricingError,
    normalize_pricing_model,
    validate_pricing_config,
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
) -> str:
    """
    Insert a new agent listing. Returns the agent_id.
    Pass agent_id explicitly for deterministic IDs (e.g. self-registration).
    By default this also writes an embedding row in the same request.
    Raises sqlite3.IntegrityError if agent_id already exists.
    """
    try:
        price = float(price_per_call_usd)
    except (TypeError, ValueError):
        raise ValueError("price_per_call_usd must be a non-negative number.")
    if not math.isfinite(price):
        raise ValueError("price_per_call_usd must be a finite non-negative number.")
    if price < 0:
        raise ValueError("price_per_call_usd must be non-negative.")

    aid = agent_id or str(uuid.uuid4())
    normalized_owner_id = (owner_id or f"agent:{aid}").strip()
    if not normalized_owner_id:
        raise ValueError("owner_id must be a non-empty string.")
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
        normalized_examples: str | None = json.dumps(
            [ex for ex in output_examples if isinstance(ex, dict)]
        ) or None
    else:
        normalized_examples = None
    normalized_verified = 1 if verified else 0
    normalized_health_status = str(endpoint_health_status or "unknown").strip().lower()
    if normalized_health_status not in {"unknown", "healthy", "degraded"}:
        raise ValueError("endpoint_health_status must be one of: unknown, healthy, degraded.")
    normalized_status = str(status or "active").strip().lower()
    if normalized_status not in {"active", "suspended", "banned"}:
        raise ValueError("status must be one of: active, suspended, banned.")
    is_internal = internal_only or str(endpoint_url or "").strip().startswith("internal://")
    normalized_review_status = str(review_status or "").strip().lower()
    if not normalized_review_status:
        normalized_review_status = "approved" if is_internal else "pending_review"
    if normalized_review_status not in REVIEW_STATUSES:
        raise ValueError(
            "review_status must be one of: " + ", ".join(sorted(REVIEW_STATUSES)) + "."
        )
    normalized_review_note = str(review_note or "").strip() or None
    normalized_reviewed_at = str(reviewed_at or "").strip() or None
    normalized_reviewed_by = str(reviewed_by or "").strip() or None
    normalized_decay_multiplier = _to_non_negative_float(trust_decay_multiplier, default=1.0)
    if normalized_decay_multiplier <= 0:
        normalized_decay_multiplier = 1.0
    internal_only_int = 1 if internal_only else 0
    try:
        normalized_pricing_model = normalize_pricing_model(pricing_model)
    except VariablePricingError as exc:
        raise ValueError(str(exc))
    pricing_config_json: str | None = None
    if normalized_pricing_model != "fixed":
        try:
            canonical_config = validate_pricing_config(
                normalized_pricing_model, pricing_config
            )
        except VariablePricingError as exc:
            raise ValueError(str(exc))
        if canonical_config is not None:
            pricing_config_json = json.dumps(canonical_config, sort_keys=True)
    source_text = ""
    embedding_vector: list[float] | None = None
    if embed_listing:
        source_text = _build_embedding_source_text(name, description, normalized_tags, normalized_schema)
        embedding_vector = embeddings.embed_text(source_text)
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agents
                (agent_id, owner_id, name, description, endpoint_url, healthcheck_url,
                 price_per_call_usd, tags, input_schema, output_schema, output_verifier_url,
                 output_examples, verified, endpoint_health_status, endpoint_consecutive_failures,
                 endpoint_last_checked_at, endpoint_last_error,
                 internal_only, status, review_status, review_note, reviewed_at, reviewed_by,
                 trust_decay_multiplier, last_decay_at, created_at,
                 model_provider, model_id, pricing_model, pricing_config)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        if embed_listing and embedding_vector is not None:
            _upsert_agent_embedding_row(
                conn,
                agent_id=aid,
                source_text=source_text,
                embedding_vector=embedding_vector,
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
            SET pricing_model = ?, pricing_config = ?
            WHERE agent_id = ?
            """,
            (normalized_model, serialized, agent_id),
        ).rowcount
    if updated == 0:
        return None
    return get_agent(agent_id, include_unapproved=True)


def update_call_stats(agent_id: str, latency_ms: float, success: bool) -> None:
    """
    Increment total_calls, update running avg_latency_ms, and conditionally
    increment successful_calls. Uses a single UPDATE with arithmetic to avoid
    a read-modify-write race.
    """
    with _conn() as conn:
        conn.execute(
            """
            UPDATE agents
            SET total_calls    = total_calls + 1,
                avg_latency_ms = (avg_latency_ms * total_calls + ?) / (total_calls + 1),
                successful_calls = successful_calls + ?
            WHERE agent_id = ?
            """,
            (latency_ms, 1 if success else 0, agent_id),
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
            where_clauses.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        if normalized_provider:
            where_clauses.append("model_provider = ?")
            params.append(normalized_provider)
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        rows = conn.execute(
            f"SELECT * FROM agents {where_sql} ORDER BY created_at",
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_agent_status(agent_id: str, status: str) -> dict | None:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"active", "suspended", "banned"}:
        raise ValueError("status must be one of: active, suspended, banned.")
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET status = ? WHERE agent_id = ?",
            (normalized_status, agent_id),
        )
    return get_agent(agent_id, include_unapproved=True)


def list_pending_review_agents(limit: int = 200) -> list[dict]:
    capped = min(max(1, int(limit)), 1000)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM agents
            WHERE review_status = 'pending_review'
            ORDER BY created_at DESC, agent_id DESC
            LIMIT ?
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
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approve", "reject"}:
        raise ValueError("decision must be one of: approve, reject.")
    target_status = "approved" if normalized_decision == "approve" else "rejected"
    normalized_reviewed_by = str(reviewed_by or "").strip()
    if not normalized_reviewed_by:
        raise ValueError("reviewed_by must be a non-empty string.")
    normalized_note = str(note or "").strip() or None
    normalized_reviewed_at = str(reviewed_at or datetime.now(timezone.utc).isoformat()).strip()
    with _conn() as conn:
        updated = conn.execute(
            """
            UPDATE agents
            SET review_status = ?,
                review_note = ?,
                reviewed_at = ?,
                reviewed_by = ?
            WHERE agent_id = ?
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
            "UPDATE agents SET verified = ? WHERE agent_id = ?",
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
    normalized_status = str(endpoint_health_status or "").strip().lower()
    if normalized_status not in {"unknown", "healthy", "degraded"}:
        raise ValueError("endpoint_health_status must be one of: unknown, healthy, degraded.")
    normalized_failures = _to_non_negative_int(endpoint_consecutive_failures, default=0)
    checked_at = str(endpoint_last_checked_at or "").strip()
    if not checked_at:
        raise ValueError("endpoint_last_checked_at must be a non-empty ISO timestamp.")
    normalized_error = str(endpoint_last_error or "").strip() or None
    with _conn() as conn:
        conn.execute(
            """
            UPDATE agents
            SET endpoint_health_status = ?,
                endpoint_consecutive_failures = ?,
                endpoint_last_checked_at = ?,
                endpoint_last_error = ?
            WHERE agent_id = ?
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
                WHERE agent_id = ? AND status = 'active'
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
                WHERE agent_id = ? AND status = 'suspended' AND suspension_reason = 'health_check'
                """,
                (agent_id,),
            )
            if result.rowcount:
                _logger.info(
                    "agent %s reinstated after endpoint health recovery",
                    agent_id,
                )
    return get_agent(agent_id, include_unapproved=True)


def set_agent_output_examples(agent_id: str, output_examples: list[dict] | None) -> dict | None:
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
            "UPDATE agents SET output_examples = ? WHERE agent_id = ?",
            (normalized_examples, agent_id),
        )
    return get_agent(agent_id, include_unapproved=True)


def append_agent_output_example(agent_id: str, example: dict, *, max_examples: int = 20) -> dict | None:
    if not isinstance(example, dict):
        raise ValueError("example must be an object.")
    capped = min(max(1, int(max_examples)), 100)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT output_examples FROM agents WHERE agent_id = ?",
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
                    parsed_examples = [item for item in loaded if isinstance(item, dict)]
            except (TypeError, json.JSONDecodeError):
                parsed_examples = []
        next_examples = [example] + parsed_examples
        if len(next_examples) > capped:
            next_examples = next_examples[:capped]
        conn.execute(
            "UPDATE agents SET output_examples = ? WHERE agent_id = ?",
            (json.dumps(next_examples), agent_id),
        )
    return get_agent(agent_id, include_unapproved=True)


def touch_agent_decay(agent_id: str, at_iso: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET last_decay_at = ? WHERE agent_id = ?",
            (str(at_iso or _CANONICAL_CREATED_AT), agent_id),
        )


def set_agent_decay_multiplier(agent_id: str, multiplier: float, at_iso: str) -> None:
    parsed = _to_non_negative_float(multiplier, default=1.0)
    parsed = max(0.0, min(1.0, parsed))
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET trust_decay_multiplier = ?, last_decay_at = ? WHERE agent_id = ?",
            (parsed, str(at_iso or _CANONICAL_CREATED_AT), agent_id),
        )


def get_agent(agent_id: str, *, include_unapproved: bool = True) -> dict | None:
    """Return a single agent listing by ID, or None if not found."""
    where_sql = "agent_id = ?"
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
            "SELECT * FROM agents WHERE owner_id = ? ORDER BY created_at",
            (owner_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_owner_agents(owner_id: str) -> int:
    """Return the number of non-deleted agents owned by this user."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE owner_id = ? AND (status IS NULL OR status != 'deleted')",
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
) -> dict | None:
    """
    Update mutable fields on an agent. Only the owner can call this.
    Returns the updated agent dict, or None if not found / wrong owner.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = ? AND owner_id = ?",
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
                raise ValueError("price_per_call_usd must be a non-negative finite number.")
            updates["price_per_call_usd"] = price

        if not updates:
            return agent

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [agent_id, owner_id]
        conn.execute(
            f"UPDATE agents SET {set_clause} WHERE agent_id = ? AND owner_id = ?",
            values,
        )
        updated_row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
    return _row_to_dict(updated_row) if updated_row else None


def delist_agent(agent_id: str, owner_id: str) -> bool:
    """
    Soft-delete an agent by setting status='deleted'. Only the owner can do this.
    Returns True if found and updated, False if not found or wrong owner.
    """
    with _conn() as conn:
        result = conn.execute(
            "UPDATE agents SET status = 'deleted' WHERE agent_id = ? AND owner_id = ? AND status != 'deleted'",
            (agent_id, owner_id),
        )
    return result.rowcount > 0


def update_agent_health(agent_id: str, status: str, checked_at: str) -> None:
    """Record the result of a health check probe."""
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET last_health_status = ?, last_health_check_at = ? WHERE agent_id = ?",
            (status, checked_at, agent_id),
        )


def agent_exists_by_name(name: str) -> bool:
    """Return True if any agent with this name is already registered."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM agents WHERE name = ?", (name,)
        ).fetchone()
    return row is not None


def sync_agent_embedding(agent_id: str) -> bool:
    """
    Re-embed one agent if its current source_text has changed.
    This is the helper future update paths should call after mutating
    name/description/tags/input_schema.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?",
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
            query += " LIMIT ?"
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


def _matched_phrase(query: str, haystack: str) -> str | None:
    terms = _query_terms(query)
    if not terms:
        return None

    lowered = haystack.lower()
    for width in (3, 2):
        if len(terms) < width:
            continue
        for idx in range(0, len(terms) - width + 1):
            phrase = " ".join(terms[idx: idx + width])
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
    if required_fields:
        if len(required_fields) == 1:
            field = sorted(required_fields)[0]
            reasons.append(f"supports {field} input field")
        else:
            ordered = ", ".join(sorted(supported_fields))
            reasons.append(f"supports input fields: {ordered}")
    reasons.append(f"trust {trust:.2f}")
    if caller_trust is not None and caller_trust_min is not None:
        reasons.append(f"caller trust {caller_trust:.2f} meets minimum {caller_trust_min:.2f}")
    return reasons


def search_agents(
    query: str,
    limit: int = 10,
    min_trust: float = 0.0,
    max_price_cents: int | None = None,
    required_input_fields: list[str] | None = None,
    caller_trust: float | None = None,
    include_unapproved: bool = True,
    model_provider: str | None = None,
) -> list[dict]:
    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ValueError("query must be a non-empty string.")
    if limit < 1:
        raise ValueError("limit must be >= 1.")
    if max_price_cents is not None and max_price_cents < 0:
        raise ValueError("max_price_cents must be >= 0 when provided.")
    normalized_model_provider = str(model_provider or "").strip().lower() or None

    trust_floor = _normalize_min_trust(min_trust)
    normalized_caller_trust = None
    if caller_trust is not None:
        normalized_caller_trust = _normalize_min_trust(caller_trust)
    required_fields = _required_input_fields_set(required_input_fields)
    query_vector = np.asarray(embeddings.embed_text(normalized_query), dtype=np.float32)
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

        if normalized_model_provider and agent.get("model_provider") != normalized_model_provider:
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

        vector = vectors_by_agent.get(agent_id)
        if vector is None:
            source_text = _embedding_source_from_agent(agent)
            vector_list = embeddings.embed_text(source_text)
            vector = np.asarray(vector_list, dtype=np.float32)
            vectors_by_agent[agent_id] = vector
            missing_embeddings.append((agent_id, source_text, vector_list))

        similarity = float(embeddings.cosine(query_vector, vector))
        semantic_similarity = max(0.0, min(1.0, similarity))
        candidates.append(
            {
                "agent": agent,
                "similarity": semantic_similarity,
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
            normalized_price = (candidate["price_cents"] - min_price) / (max_price - min_price)
            inverse_price = 1.0 - normalized_price

        blended_score = (
            SEMANTIC_SIMILARITY_WEIGHT * candidate["similarity"]
            + TRUST_SCORE_WEIGHT * candidate["trust"]
            + INVERSE_PRICE_WEIGHT * inverse_price
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

    return [
        {
            "agent": item["agent"],
            "similarity": round(item["similarity"], 6),
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


def get_agent_with_reputation(agent_id: str, *, include_unapproved: bool = True) -> dict | None:
    """Return one enriched listing by agent_id, or None if missing."""
    from core import reputation

    agent = get_agent(agent_id, include_unapproved=include_unapproved)
    return reputation.enrich_agent_record(agent) if agent else None
