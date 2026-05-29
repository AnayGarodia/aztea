"""
decision_audit.py — fire-and-forget persistence of auto-hire decisions.

OWNS: writes one row to ``auto_hire_decisions`` per ``do_specialist_task`` /
      ``registry_auto_hire`` request, capturing the intent, gating outcome,
      chosen agent, and (when applicable) the resulting job_id.
NOT OWNS: the decision logic itself (``core/registry/auto_hire.py``) and the
      route-level response shape (``server/application_parts/part_012.py``).
INVARIANTS:
  - ``record_decision`` never raises. A write failure is logged at debug level
    and dropped — the request must not be blocked on observability.
  - The persistence path is asynchronous: ``record_decision`` enqueues the
    INSERT onto ``core.deferred`` and returns immediately. The deferred
    worker drains. Loss bound = items in-queue at crash time (best-effort).
DECISIONS:
  - Intent text is truncated to keep rows lean; ``intent_hash`` is a SHA-256
    of the full original text so identical intents cluster cleanly.
  - The candidates JSON is capped at the ``Decision.candidates`` top-N already
    set by ``auto_hire.decide`` (currently 3). Anything richer goes to logs.
  - When the deferred queue is unavailable (test runs, shutdown), we fall
    through to a direct sync write so the audit row still lands.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from core import db as _db
from core import deferred as _deferred

logger = logging.getLogger(__name__)

# Why named: the column is TEXT NOT NULL so a missing/empty intent would crash
# the insert. We cap and substitute defensively at the boundary.
_INTENT_TEXT_TRUNCATE = 4096


def _hash_intent(intent_text: str) -> str:
    """Stable SHA-256 of the full intent so identical phrasing groups cleanly."""
    return hashlib.sha256(intent_text.encode("utf-8", errors="replace")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_row(params: tuple[Any, ...]) -> None:
    """Synchronous INSERT. Called either from the request thread (fallback)
    or from the deferred worker. Never raises out of the worker.

    ``params`` is the 16-tuple matching the migration-0068 column order
    (see ``record_decision``). On a "no such column" failure — i.e. an
    environment where migration 0068 hasn't been applied — we drop the
    three forward-only columns and retry the legacy 13-column INSERT so
    the audit row still lands.
    """
    try:
        conn: _db.DbConnection = _db.get_raw_connection(_db.DB_PATH)
        try:
            conn.execute(
                """
                INSERT INTO auto_hire_decisions (
                    decision_id, caller_owner_id, caller_key_id,
                    intent_text, intent_hash, auto_invoked, dry_run, reason,
                    chosen_agent_id, confidence, candidates_json,
                    resulting_job_id, feature_vector_json,
                    shadow_chosen_agent_id, intent_class, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                params,
            )
        except _db.OperationalError as exc:
            msg = str(exc).lower()
            if "no such column" in msg or "undefined column" in msg:
                # Migration 0068 not applied yet. Drop the 3 forward-only
                # columns (indices 12..14) and write the legacy column
                # set. Preserves auditability in dev envs / partial
                # rollouts.
                legacy_params = params[:12] + (params[15],)
                conn.execute(
                    """
                    INSERT INTO auto_hire_decisions (
                        decision_id, caller_owner_id, caller_key_id,
                        intent_text, intent_hash, auto_invoked, dry_run, reason,
                        chosen_agent_id, confidence, candidates_json,
                        resulting_job_id, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    legacy_params,
                )
            else:
                raise
        conn.commit()
    except _db.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            logger.debug("decision_audit: write failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 — never block the worker
        logger.debug("decision_audit: write failed: %s", exc)


def record_decision(
    *,
    intent_text: str,
    auto_invoked: bool,
    dry_run: bool = False,
    reason: str | None = None,
    chosen_agent_id: str | None = None,
    confidence: float | None = None,
    candidates: list[dict[str, Any]] | None = None,
    caller_owner_id: str | None = None,
    caller_key_id: str | None = None,
    resulting_job_id: str | None = None,
    feature_vector: dict[str, Any] | None = None,
    shadow_chosen_agent_id: str | None = None,
    intent_class: str | None = None,
) -> str | None:
    """Enqueue one auto-hire decision write. Returns the decision_id.

    Why: the gated reasons (no_match, insufficient_confidence, price_exceeded,
    insufficient_trust, ...) are only visible in the HTTP response otherwise,
    which makes "top no-match intents" and "fraction gated vs auto-invoked"
    impossible to answer.

    The write itself happens in the deferred-queue worker, NOT inline. The
    caller gets the decision_id back immediately so the response can reference
    it. If enqueue overflows or the queue is not started (e.g. tests), the
    INSERT falls through to a direct synchronous write.

    Phase 3.5 (2026-05-28): ``feature_vector``, ``shadow_chosen_agent_id``,
    and ``intent_class`` are forward-only logging columns added in
    migration 0068. Write-only — no current code path reads them. They
    accumulate so Phase 4's learned-ranker backtest has data. The
    ``_write_row`` helper falls back to a 13-column INSERT when migration
    0068 is missing, so callers in partial-rollout envs still record.
    """
    decision_id = uuid.uuid4().hex
    safe_intent = (intent_text or "")[:_INTENT_TEXT_TRUNCATE]
    intent_hash = _hash_intent(intent_text or "")
    candidates_json = json.dumps(candidates or [], default=str)
    feature_vector_json = (
        json.dumps(feature_vector, default=str)
        if feature_vector is not None
        else None
    )
    params = (
        decision_id,
        caller_owner_id,
        caller_key_id,
        safe_intent,
        intent_hash,
        1 if auto_invoked else 0,
        1 if dry_run else 0,
        reason,
        chosen_agent_id,
        confidence,
        candidates_json,
        resulting_job_id,
        feature_vector_json,
        shadow_chosen_agent_id,
        intent_class,
        _now_iso(),
    )
    enqueued = _deferred.enqueue(
        "decision_audit",
        _write_row,
        params,
        caller_owner_id=caller_owner_id,
    )
    if not enqueued:
        # Queue is full OR worker not started — fall back to sync write so
        # the audit row still lands. Slightly slower on the response path
        # but preserves auditability.
        _write_row(params)
    return decision_id
