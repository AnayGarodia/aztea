"""Typed job messages, correlation tracking, and quality / dispute state writes.

This module owns two related concerns:

1. **Structured job messages** — progress, clarifications, typed agent
   messages, tool calls, tool results, artifacts, and partial results. Each
   message type has different effects on the lease (for example,
   ``clarification_request`` sets the job to ``awaiting_clarification`` and
   relaxes heartbeats; ``clarification_response`` resumes ``running``). See
   ``_resolve_message_lease_behavior`` imported from ``leases`` for the
   table.

2. **Quality and dispute outcome writes** — ``set_job_quality_result`` and
   ``set_job_dispute_outcome`` persist the final judgment fields on the
   ``jobs`` row. These live here (not in ``db``) because they read the
   updated row back via ``get_job`` from ``crud`` — the quality judge writes
   are the one place where the three split submodules all meet.

The earlier monolithic ``core/jobs.py`` used to contain duplicate versions of
these helpers that referenced an unresolved ``get_job``; those duplicates
have been removed and this module is the single source of truth.
"""

from __future__ import annotations

from typing import Any

from core import job_events as _job_events
from core import models as _models

from .crud import get_job
from .db import (
    _ACTIVE_CLAIM_EVENT_TYPES,
    _CLAIM_EVENT_MSG_TYPE,
    _LEASE_BEHAVIOR_EXTEND,
    _LEASE_BEHAVIOR_EXTEND_AND_MARK_AWAITING,
    _LEASE_BEHAVIOR_EXTEND_AND_RESUME_RUNNING,
    DEFAULT_LEASE_SECONDS,
    _claim_token_sha256,
    _clean_optional_text,
    _conn,
    _decode_json,
    _insert_claim_event_row,
    _insert_job_message_row,
    _models,
    _msg_to_dict,
    _now,
    _now_dt,
    _parse_ts,
    _publish_job_message,
    _to_non_negative_int,
    timedelta,
)
from .leases import (
    _message_correlation_exists_conn,
    _resolve_message_lease_behavior,
)

import json as _json
import logging as _logging

_LOG = _logging.getLogger(__name__)


class JobAlreadyTerminal(Exception):
    """Raised when partial_output or steer is sent to an already-terminal job.

    Routes translate this to HTTP 409 (job.terminal). Surfacing it as its
    own type keeps the messaging tx atomic and lets callers distinguish
    "you're racing the stop" from "your input is malformed".
    """


_LEASE_EXTENDING_BEHAVIORS = frozenset({
    _LEASE_BEHAVIOR_EXTEND,
    _LEASE_BEHAVIOR_EXTEND_AND_MARK_AWAITING,
    _LEASE_BEHAVIOR_EXTEND_AND_RESUME_RUNNING,
})

# Single transactional UPDATE so lease + status + clarification deadlines
# all commit atomically; a partial application would corrupt lease state.
_JOB_MESSAGE_SIDE_EFFECT_SQL = """
    UPDATE jobs
    SET status = CASE WHEN %s = 1 THEN %s ELSE status END,
        lease_expires_at = CASE WHEN %s = 1 THEN %s ELSE lease_expires_at END,
        last_heartbeat_at = CASE WHEN %s = 1 THEN %s ELSE last_heartbeat_at END,
        clarification_requested_at = CASE
            WHEN %s = 1 THEN %s
            WHEN %s = 1 THEN NULL
            ELSE clarification_requested_at
        END,
        clarification_deadline_at = CASE
            WHEN %s = 1 THEN %s
            WHEN %s = 1 THEN NULL
            ELSE clarification_deadline_at
        END,
        updated_at = %s
    WHERE job_id = %s
"""


def _resolve_status_target(lease_behavior: str) -> str | None:
    """Pure: status to transition to for clarification-related lease behaviours."""
    if lease_behavior == _LEASE_BEHAVIOR_EXTEND_AND_MARK_AWAITING:
        return "awaiting_clarification"
    if lease_behavior == _LEASE_BEHAVIOR_EXTEND_AND_RESUME_RUNNING:
        return "running"
    return None


def _validate_tool_result_correlation(
    conn: Any, *, job_id: str, canonical_type: str,
    normalized_correlation_id: str | None,
) -> None:
    """Side-effect: enforce tool_result invariant — must reference an existing tool_call."""
    if canonical_type != "tool_result":
        return
    if normalized_correlation_id is None:
        raise ValueError("tool_result messages require a correlation_id.")
    if not _message_correlation_exists_conn(
        conn,
        job_id=job_id,
        correlation_id=normalized_correlation_id,
        msg_type="tool_call",
    ):
        raise ValueError(
            f"tool_result correlation_id '{normalized_correlation_id}' has no matching tool_call."
        )


def _next_lease_expiry(
    raw: dict, *, lease_seconds: int, now_dt: Any,
) -> str:
    """Pure: compute the new ``lease_expires_at`` ISO string from current state."""
    existing_expiry = _parse_ts(_clean_optional_text(raw.get("lease_expires_at")))
    base_dt = (
        existing_expiry
        if existing_expiry and existing_expiry > now_dt
        else now_dt
    )
    return (base_dt + timedelta(seconds=lease_seconds)).isoformat()


def _clarification_deadline_at(
    raw: dict, *, now_dt: Any, mark_clarification_requested: bool,
) -> str | None:
    """Pure: deadline ISO string when a clarification request opens, else None."""
    if not mark_clarification_requested:
        return None
    timeout_seconds = _to_non_negative_int(
        raw.get("clarification_timeout_seconds"), default=0,
    )
    if timeout_seconds <= 0:
        return None
    return (now_dt + timedelta(seconds=timeout_seconds)).isoformat()


def _compute_side_effect_flags(
    raw: dict, *, canonical_type: str, status_target: str | None,
    should_extend_lease: bool,
) -> dict[str, Any]:
    """Pure: derive the boolean flags that drive ``_JOB_MESSAGE_SIDE_EFFECT_SQL``."""
    completed = _clean_optional_text(raw.get("completed_at")) is not None
    settled = _clean_optional_text(raw.get("settled_at")) is not None
    claim_owner_id = _clean_optional_text(raw.get("claim_owner_id"))
    claim_token = _clean_optional_text(raw.get("claim_token"))
    return {
        "should_update_status": (
            status_target is not None and not completed and not settled
        ),
        "mark_clarification_requested": canonical_type == "clarification_request",
        "clear_clarification_tracking": canonical_type == "clarification_response",
        "lease_extended": bool(
            should_extend_lease
            and claim_owner_id is not None
            and claim_token is not None
        ),
        "claim_owner_id": claim_owner_id,
        "claim_token": claim_token,
    }


def _build_side_effect_params(
    flags: dict[str, Any], *, status_target: str | None, now: str,
    next_lease_expires_at: str | None, deadline_at: str | None, job_id: str,
) -> tuple:
    """Pure: positional args matching ``_JOB_MESSAGE_SIDE_EFFECT_SQL`` placeholders."""
    return (
        1 if flags["should_update_status"] else 0, status_target,
        1 if flags["lease_extended"] else 0, next_lease_expires_at,
        1 if flags["lease_extended"] else 0, now,
        1 if flags["mark_clarification_requested"] else 0, now,
        1 if flags["clear_clarification_tracking"] else 0,
        1 if flags["mark_clarification_requested"] else 0, deadline_at,
        1 if flags["clear_clarification_tracking"] else 0,
        now, job_id,
    )


def _apply_message_side_effects_to_job(
    conn: Any, *, job_id: str, raw: dict, now: str,
    canonical_type: str, status_target: str | None,
    should_extend_lease: bool, lease_seconds: int, now_dt: Any,
) -> tuple[bool, str | None, str | None, str | None]:
    """Side-effect: atomically apply lease/status/deadline updates for a message."""
    flags = _compute_side_effect_flags(
        raw, canonical_type=canonical_type, status_target=status_target,
        should_extend_lease=should_extend_lease,
    )
    next_lease_expires_at = (
        _next_lease_expiry(raw, lease_seconds=lease_seconds, now_dt=now_dt)
        if flags["lease_extended"] else None
    )
    deadline_at = _clarification_deadline_at(
        raw, now_dt=now_dt,
        mark_clarification_requested=flags["mark_clarification_requested"],
    )
    has_change = (
        flags["should_update_status"] or flags["lease_extended"]
        or flags["mark_clarification_requested"]
        or flags["clear_clarification_tracking"]
    )
    if has_change:
        conn.execute(_JOB_MESSAGE_SIDE_EFFECT_SQL, _build_side_effect_params(
            flags, status_target=status_target, now=now,
            next_lease_expires_at=next_lease_expires_at,
            deadline_at=deadline_at, job_id=job_id,
        ))
    return (
        flags["lease_extended"], next_lease_expires_at,
        flags["claim_owner_id"], flags["claim_token"],
    )


def _apply_lease_effects_within_txn(
    conn: Any, *, job_id: str, row: Any, now: str, now_dt: Any,
    canonical_type: str, normalized_type: str, from_id: str,
    status_target: str | None, should_extend_lease: bool,
    lease_seconds: int,
) -> None:
    """Side-effect: mutate jobs row + log claim event when message has lease/status side-effects."""
    raw = dict(row)
    lease_extended, next_lease_expires_at, claim_owner_id, claim_token = (
        _apply_message_side_effects_to_job(
            conn, job_id=job_id, raw=raw, now=now,
            canonical_type=canonical_type, status_target=status_target,
            should_extend_lease=should_extend_lease,
            lease_seconds=lease_seconds, now_dt=now_dt,
        )
    )
    if not lease_extended:
        return
    _insert_claim_event_row(
        conn, job_id, event_type="claim_lease_extended",
        claim_owner_id=claim_owner_id, claim_token=claim_token,
        lease_started_at=now, lease_expires_at=next_lease_expires_at,
        actor_id=from_id,
        metadata={
            "message_type": normalized_type,
            "canonical_message_type": canonical_type,
            "status_after": status_target if status_target else raw.get("status"),
        },
        created_at=now,
    )


def _normalize_message_for_insert(
    msg_type: str, payload: dict, correlation_id: str | None,
) -> dict[str, Any]:
    """Pure: shape caller inputs into the normalised form ``add_message`` writes to DB."""
    normalized = _models.normalize_job_message_body(
        msg_type=msg_type, payload=payload,
        correlation_id=correlation_id, allow_legacy=True,
    )
    normalized_type = normalized["type"]
    canonical_type = normalized["canonical_type"]
    lease_behavior = _resolve_message_lease_behavior(normalized_type, canonical_type)
    return {
        "normalized_type": normalized_type,
        "canonical_type": canonical_type,
        "normalized_payload": normalized["payload"],
        "normalized_correlation_id": _clean_optional_text(normalized.get("correlation_id")),
        "should_extend_lease": lease_behavior in _LEASE_EXTENDING_BEHAVIORS,
        "status_target": _resolve_status_target(lease_behavior),
    }


_COPILOT_SIDE_EFFECT_TYPES = ("partial_output", "steer")


def _guard_terminal_for_copilot(row: dict, *, job_id: str, canonical_type: str) -> None:
    """Pure-ish: refuse partial_output / steer once the job is terminal.

    Why: an in-flight client could otherwise squeeze a steer in after the
    job was already stopped by a stop_when match — making "stopped" not
    actually terminal. This is the only ordering guard that matters for
    copilot side-effects.

    1.6.9: previously checked only ``terminal_at`` (the stop_when path's
    timestamp). Normally-completed jobs have ``completed_at`` set but no
    ``terminal_at`` — so a steer arriving on a complete job slipped past
    this guard, hit a downstream DB error, and returned HTTP 500 to the
    caller. Per the documented contract, both paths must return 409
    job.terminal. Now we check the broader terminal-status set first.
    """
    if canonical_type not in _COPILOT_SIDE_EFFECT_TYPES:
        return
    raw_status = str(row.get("status") or "").strip().lower()
    _TERMINAL_STATUSES = {
        "complete", "completed", "stopped", "failed", "cancelled", "expired",
    }
    if raw_status in _TERMINAL_STATUSES:
        raise JobAlreadyTerminal(
            f"job {job_id} is terminal (status={raw_status}); cannot accept {canonical_type}"
        )
    if _clean_optional_text(row.get("terminal_at")) is not None:
        raise JobAlreadyTerminal(
            f"job {job_id} is terminal; cannot accept {canonical_type}"
        )


def _insert_message_within_txn(
    conn: Any, *, job_id: str, from_id: str, n: dict[str, Any], now: str, now_dt: Any,
    lease_seconds: int,
) -> tuple[int, str | None]:
    """Side-effect: validate + insert + apply side-effects; returns (message_id, copilot_terminal_state)."""
    _validate_tool_result_correlation(
        conn, job_id=job_id, canonical_type=n["canonical_type"],
        normalized_correlation_id=n["normalized_correlation_id"],
    )
    row = conn.execute(
        "SELECT * FROM jobs WHERE job_id = %s", (job_id,),
    ).fetchone()
    if row is not None:
        _guard_terminal_for_copilot(
            dict(row), job_id=job_id, canonical_type=n["canonical_type"],
        )
    message_id = _insert_job_message_row(
        conn, job_id=job_id, from_id=from_id,
        msg_type=n["normalized_type"], payload=n["normalized_payload"],
        correlation_id=n["normalized_correlation_id"], created_at=now,
    )
    copilot_terminal_state: str | None = None
    if row is not None and n["canonical_type"] in _COPILOT_SIDE_EFFECT_TYPES:
        copilot_terminal_state, _ = _apply_copilot_side_effects(
            conn, job_id=job_id, canonical_type=n["canonical_type"],
            payload=n["normalized_payload"], message_id=message_id,
            now=now, row=dict(row),
        )
    if row is not None and (n["should_extend_lease"] or n["status_target"] is not None):
        _apply_lease_effects_within_txn(
            conn, job_id=job_id, row=row, now=now, now_dt=now_dt,
            canonical_type=n["canonical_type"], normalized_type=n["normalized_type"],
            from_id=from_id, status_target=n["status_target"],
            should_extend_lease=n["should_extend_lease"], lease_seconds=lease_seconds,
        )
    return message_id, copilot_terminal_state


def add_message(
    job_id: str,
    from_id: str,
    msg_type: str,
    payload: dict,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    correlation_id: str | None = None,
) -> dict:
    """Side-effect: insert a typed job message and apply any lease/status side-effects.

    Why: 'progress' extends the lease; 'clarification_request' transitions
    to awaiting_clarification; 'clarification_response' resumes running.
    Lease, status, and deadline updates commit atomically with the insert.
    """
    from core.jobs.db import _validate_lease_seconds
    _validate_lease_seconds(lease_seconds)
    n = _normalize_message_for_insert(msg_type, payload, correlation_id)
    now_dt = _now_dt()
    now = now_dt.isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        message_id, copilot_terminal_state = _insert_message_within_txn(
            conn, job_id=job_id, from_id=from_id, n=n,
            now=now, now_dt=now_dt, lease_seconds=lease_seconds,
        )
    # 1.7.2 — build the return dict from in-scope values rather than
    # round-tripping through get_message. The post-commit get_message
    # was racing with read-after-write visibility on the thread-local
    # SQLite connection: the steer was persisted (steer_count
    # incremented), but get_message returned None, which the route
    # then surfaced as a 409 "could not be persisted" — the response
    # and the DB state contradicted each other (B-2/N4 in the 1.7.1
    # eval). We just inserted these exact bytes; reconstruct directly.
    message = {
        "message_id": message_id,
        "job_id": job_id,
        "from_id": from_id,
        "type": n["normalized_type"],
        "payload": n["normalized_payload"],
        "correlation_id": n["normalized_correlation_id"],
        "created_at": now,
    }
    _publish_job_message(job_id, message)
    _maybe_notify_elixir_message(job_id, message)

    # Synchronous post-commit drain: if this message caused a stop_when match,
    # the messaging tx already enqueued the pending_settlements row. Drain it
    # now so the caller sees ledger settlement and (in Phase 3) the signed
    # receipt without a separate poll.
    if copilot_terminal_state == "stopped":
        try:
            from core import settlement_runner

            settlement_runner.drain_one(job_id=job_id)
        except Exception:
            _LOG.exception(
                "settlement_runner.sync_drain_failed", extra={"job_id": job_id}
            )

    return message


def _apply_copilot_side_effects(
    conn,
    *,
    job_id: str,
    canonical_type: str,
    payload: dict,
    message_id: int,
    now: str,
    row: dict,
) -> tuple[str | None, dict | None]:
    """Counter increments, stop_when checks, terminal stamping, settlement enqueue.

    Runs inside the messaging tx. Returns (terminal_state, stop_reason) so the
    caller can trigger a synchronous settlement drain after commit.

    For partial_output: increments partials_count; if any registered
    stop_when predicate matches, stamps terminal_at + terminal_message_id +
    stop_reason_json + status='stopped' on the job and inserts a
    pending_settlements row.

    For steer: increments steer_count only. (Steers themselves never
    terminate the job.)
    """
    if canonical_type == "steer":
        conn.execute(
            "UPDATE jobs SET steer_count = COALESCE(steer_count, 0) + 1, updated_at = %s WHERE job_id = %s",
            (now, job_id),
        )
        return None, None

    conn.execute(
        "UPDATE jobs SET partials_count = COALESCE(partials_count, 0) + 1, updated_at = %s WHERE job_id = %s",
        (now, job_id),
    )

    stop_when_raw = row.get("stop_when_json")
    if not stop_when_raw:
        return None, None

    predicates = _parse_stop_when_for_eval(stop_when_raw)
    if not predicates:
        return None, None

    # Run predicates against the inner free-form dict — the pydantic model
    # wraps it under {payload: {...}}, but the user-facing semantics is "the
    # dict the agent emitted." So we target payload["payload"] when present,
    # falling back to the whole payload otherwise.
    target = payload.get("payload") if isinstance(payload, dict) else payload
    if target is None:
        target = payload

    from core.copilot_predicates import evaluate_first_match

    match = evaluate_first_match(predicates, target)
    if match is None:
        return None, None

    stop_reason = {
        "label": match.get("label"),
        "expr": match.get("expr"),
        "matched_message_id": message_id,
        "matched_at": now,
    }
    conn.execute(
        """
        UPDATE jobs
           SET status = 'stopped',
               terminal_at = %s,
               terminal_message_id = %s,
               stop_reason_json = %s,
               completed_at = COALESCE(completed_at, %s),
               updated_at = %s
         WHERE job_id = %s
        """,
        (now, message_id, _json.dumps(stop_reason), now, now, job_id),
    )

    conn.execute(
        """
        INSERT INTO pending_settlements (job_id, terminal_state, terminal_at)
        VALUES (%s, 'stopped', %s)
        ON CONFLICT (job_id) DO NOTHING
        """,
        (job_id, now),
    )
    return "stopped", stop_reason


def _parse_stop_when_for_eval(raw: object) -> list[dict]:
    """Parse the persisted stop_when_json into a list of {label, expr} dicts.

    The persisted shape is {"predicates": [{label, expr}, ...], "max_units": ?}
    OR a bare list of predicates (older format if any). This helper tolerates
    both shapes without throwing — predicate parsing must never crash a
    partial_output insert.
    """
    if not raw:
        return []
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = _json.loads(raw)
        except (TypeError, ValueError):
            return []
    if isinstance(parsed, dict):
        preds = parsed.get("predicates") or []
        return [p for p in preds if isinstance(p, dict)]
    if isinstance(parsed, list):
        return [p for p in parsed if isinstance(p, dict)]
    return []


def add_claim_event(
    job_id: str,
    event_type: str,
    *,
    claim_owner_id: str | None = None,
    claim_token: str | None = None,
    lease_expires_at: str | None = None,
    actor_id: str | None = None,
    metadata: dict | None = None,
) -> dict | None:
    """Record a claim or heartbeat event in the job message log. Returns None if the job does not exist."""
    now = _now()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        message_id = _insert_claim_event_row(
            conn,
            job_id,
            event_type=event_type,
            claim_owner_id=claim_owner_id,
            claim_token=claim_token,
            lease_started_at=now,
            lease_expires_at=lease_expires_at,
            actor_id=actor_id,
            metadata=metadata,
            created_at=now,
        )
    message = get_message(job_id, message_id)
    _publish_job_message(job_id, message)
    return message


_CLAIM_EVENT_LOOKBACK_LIMIT = 500


def _claim_event_matches_active_token(
    payload: Any, *, owner_id: str, token_hash: str, cutoff: Any,
) -> bool:
    """Pure: True when ``payload`` is an active-claim event for this owner+token within ``cutoff``."""
    if not isinstance(payload, dict):
        return False
    if payload.get("event_type") not in _ACTIVE_CLAIM_EVENT_TYPES:
        return False
    if payload.get("claim_token_sha256") != token_hash:
        return False
    if _clean_optional_text(payload.get("claim_owner_id")) != owner_id:
        return False
    lease_expires_at = _parse_ts(_clean_optional_text(payload.get("lease_expires_at")))
    return lease_expires_at is not None and lease_expires_at >= cutoff


def claim_token_was_recently_active(
    job_id: str,
    claim_owner_id: str,
    claim_token: str,
    within_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Side-effect: True if the claim token sent a lease-extending event within ``within_seconds``."""
    owner_id = _clean_optional_text(claim_owner_id)
    token_hash = _claim_token_sha256(claim_token)
    if owner_id is None or token_hash is None or within_seconds <= 0:
        return False
    cutoff = _now_dt() - timedelta(seconds=within_seconds)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT payload
            FROM job_messages
            WHERE job_id = %s
              AND type = %s
            ORDER BY message_id DESC
            LIMIT %s
            """,
            (job_id, _CLAIM_EVENT_MSG_TYPE, _CLAIM_EVENT_LOOKBACK_LIMIT),
        ).fetchall()
    for row in rows:
        payload = _decode_json(row["payload"], default={})
        if _claim_event_matches_active_token(
            payload, owner_id=owner_id, token_hash=token_hash, cutoff=cutoff,
        ):
            return True
    return False


def get_message(job_id: str, message_id: int) -> dict | None:
    """Fetch a single job message by ``job_id`` and ``message_id``. Returns None if not found."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM job_messages
            WHERE job_id = %s AND message_id = %s
            """,
            (job_id, message_id),
        ).fetchone()
    return _msg_to_dict(row) if row else None


_MAX_MESSAGES_PER_PAGE = 200


def _build_messages_query(
    job_id: str, since_id: int | None, msg_type: str | None,
    from_id: str | None,
) -> tuple[str, list[Any]]:
    """Pure: assemble the ``WHERE``-clause SQL + params for ``get_messages``."""
    filters: list[str] = ["job_id = %s"]
    params: list[Any] = [job_id]
    if since_id is not None:
        filters.append("message_id > %s")
        params.append(since_id)
    normalized_type = (msg_type or "").strip().lower()
    if normalized_type:
        filters.append("type = %s")
        params.append(normalized_type)
    normalized_from_id = (from_id or "").strip()
    if normalized_from_id:
        filters.append("from_id = %s")
        params.append(normalized_from_id)
    return " AND ".join(filters), params


def _filter_messages_by_channel(
    messages: list[dict], channel: str | None, to_id: str | None,
) -> list[dict]:
    """Pure: post-fetch payload filtering on ``channel`` / ``to_id`` (not indexed in SQL)."""
    normalized_channel = (channel or "").strip().lower()
    normalized_to_id = (to_id or "").strip()
    if not normalized_channel and not normalized_to_id:
        return messages
    out: list[dict] = []
    for message in messages:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            continue
        if normalized_channel and str(payload.get("channel") or "").strip().lower() != normalized_channel:
            continue
        if normalized_to_id and str(payload.get("to_id") or "").strip() != normalized_to_id:
            continue
        out.append(message)
    return out


def get_messages(
    job_id: str,
    since_id: int | None = None,
    limit: int = 100,
    msg_type: str | None = None,
    from_id: str | None = None,
    channel: str | None = None,
    to_id: str | None = None,
) -> list:
    """Side-effect: paginated, ascending list of messages for a job; max 200 per page."""
    limit = min(max(1, limit), _MAX_MESSAGES_PER_PAGE)
    where_sql, params = _build_messages_query(job_id, since_id, msg_type, from_id)
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM job_messages
            WHERE {where_sql}
            ORDER BY message_id ASC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    messages = [_msg_to_dict(r) for r in rows]
    return _filter_messages_by_channel(messages, channel, to_id)


def count_job_messages(job_id: str) -> int:
    """Return the total number of messages on a job."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM job_messages WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def count_open_clarification_requests(job_id: str) -> int:
    """Count unanswered clarification_request messages on a job.

    Both queries must run inside one connection context — the prior version
    closed the conn after the first query and then re-used the closed
    handle, which blew up on Postgres and silently returned wrong counts
    on SQLite.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM job_messages WHERE job_id = %s AND type = 'clarification_request'",
            (job_id,),
        ).fetchone()
        answered = conn.execute(
            "SELECT COUNT(*) AS n FROM job_messages WHERE job_id = %s AND type = 'clarification_response'",
            (job_id,),
        ).fetchone()
    requests = int(row["n"]) if row else 0
    responses = int(answered["n"]) if answered else 0
    return max(0, requests - responses)


def get_latest_message_id(job_id: str) -> int | None:
    """Return the highest ``message_id`` for a job, or None if the job has no messages."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT MAX(message_id) AS latest_message_id
            FROM job_messages
            WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    latest = row["latest_message_id"]
    return int(latest) if latest is not None else None


def set_job_quality_result(
    job_id: str,
    *,
    judge_verdict: str,
    quality_score: int | None,
    judge_agent_id: str | None,
) -> dict | None:
    """Persist the quality judge verdict and score on the job row. Returns updated job or None."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET judge_verdict = %s, quality_score = %s, judge_agent_id = %s, updated_at = %s
            WHERE job_id = %s
            """,
            (
                _clean_optional_text(judge_verdict),
                int(quality_score) if quality_score is not None else None,
                _clean_optional_text(judge_agent_id),
                now,
                job_id,
            ),
        )
    return get_job(job_id)


def set_job_dispute_outcome(job_id: str, outcome: str | None) -> dict | None:
    """Persist the dispute outcome on the job row. Returns the updated job or None."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET dispute_outcome = %s, updated_at = %s
            WHERE job_id = %s
            """,
            (_clean_optional_text(outcome), now, job_id),
        )
    return get_job(job_id)


# Only `progress` and `partial_output` messages get forwarded to the Elixir
# realtime sidecar. Other message types (steer, clarification_request, tool
# calls, etc.) already trigger a state transition that goes through the
# _record_job_event hook, so re-emitting them would just duplicate work for
# the FE.
_REALTIME_MESSAGE_TYPES = frozenset({"progress", "partial_output"})


def _maybe_notify_elixir_message(job_id: str, message: dict) -> None:
    """Forward progress/partial_output to Elixir if the feature flag is on.

    Why a separate hook from ``_record_job_event``: messages do not write
    rows to the ``job_events`` table; they bypass the in-process SSE feed
    too. Without this, the FE would see state transitions in <1s but
    partial outputs only on the next 5s reconciliation poll. ``notify_job_event``
    is gated by AZTEA_ELIXIR_EVENTS and never raises, so the call is safe
    to perform unconditionally — but we still avoid the get_job lookup when
    the feature is off.
    """
    msg_type = message.get("type")
    if msg_type not in _REALTIME_MESSAGE_TYPES:
        return
    if not _job_events.is_enabled():
        return
    try:
        job = get_job(job_id)
        if not job:
            return
        owner_id = job.get("caller_owner_id")
        agent_owner_id = job.get("agent_owner_id")
        event_type = f"job.message.{msg_type}"
        wire_payload = {
            "message_id": message.get("message_id"),
            "payload": message.get("payload") or {},
        }
        _job_events.notify_job_event(owner_id, job_id, event_type, wire_payload)
        if agent_owner_id and agent_owner_id != owner_id:
            _job_events.notify_job_event(
                agent_owner_id, job_id, event_type, wire_payload
            )
    except Exception:  # noqa: BLE001 — best-effort; never affect lifecycle
        _LOG.warning(
            "elixir.notify_message_hook_failed",
            extra={"job_id": job_id, "msg_type": msg_type},
            exc_info=True,
        )
