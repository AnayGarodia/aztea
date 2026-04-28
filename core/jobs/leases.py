"""Job leases, claims, retries, and verification state transitions.

A lease is the window during which exactly one worker "owns" a job and may
heartbeat / complete / fail it. This module owns every transition that
changes the active lease:

- ``claim_job`` — atomically move ``pending`` → ``running`` and issue a
  cryptographically unique ``claim_token``.
- ``heartbeat_job_lease`` — extend the active lease when the worker is making
  progress.
- ``release_job_claim`` — explicit early release (e.g. worker decides it can't
  complete the job).
- ``list_jobs_with_expired_leases`` / ``_lease_is_active`` / ``_lease_is_expired``
  — scan helpers used by the background sweeper to reclaim stuck leases.
- Retry and verification-window helpers drive automatic re-queueing on lease
  expiry and the optional caller-accept / reject window before settlement.

Correlation-id bookkeeping for tool calls and streamed messages also lives
here because it shares the lease-state machine (a correlation is only valid
while the originating worker still holds the lease).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from .crud import (
    get_job,
    get_job_authorization_context,
    is_worker_authorized,
    is_worker_authorized_for_job,
)
from .db import (
    DEFAULT_LEASE_SECONDS,
    MESSAGE_TYPE_LEASE_BEHAVIOR,
    VALID_STATUSES,
    _ACTIVE_LEASE_STATUSES,
    _CLAIMABLE_STATUSES,
    _LEGACY_MESSAGE_TYPE_LEASE_BEHAVIOR,
    _clean_optional_text,
    _conn,
    _decode_json,
    _insert_claim_event_row,
    _iso_after_seconds,
    _msg_to_dict,
    _normalize_output_verification_status,
    _now,
    _now_dt,
    _parse_ts,
    _row_to_dict,
    _to_non_negative_int,
)
def _lease_is_active(job_row: dict, now_dt: datetime) -> bool:
    claim_owner_id = _clean_optional_text(job_row.get("claim_owner_id"))
    if claim_owner_id is None:
        return False
    lease_expires_at = _parse_ts(_clean_optional_text(job_row.get("lease_expires_at")))
    return bool(lease_expires_at and lease_expires_at > now_dt)


def _lease_is_expired(job_row: dict, now_dt: datetime) -> bool:
    claim_owner_id = _clean_optional_text(job_row.get("claim_owner_id"))
    if claim_owner_id is None:
        return False
    lease_expires_at = _parse_ts(_clean_optional_text(job_row.get("lease_expires_at")))
    if lease_expires_at is None:
        return True
    return lease_expires_at <= now_dt


def claim_job(
    job_id: str,
    claim_owner_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    require_authorized_owner: bool = True,
) -> dict | None:
    """Atomically move a ``pending`` job to ``running`` and issue a claim token.

    The claim token is a UUID4 embedded in the updated job row. The worker
    must present it on every subsequent heartbeat/complete/fail to prove it
    still holds the lease — preventing split-brain when a lease has been
    reclaimed by the sweeper before the original worker's network call arrives.

    Returns the updated job dict on success, or ``None`` if the job is not in
    ``pending`` state (already claimed or does not exist).
    Raises ``ValueError`` for invalid arguments.
    """
    owner_id = (claim_owner_id or "").strip()
    if not owner_id:
        raise ValueError("claim_owner_id must be a non-empty string.")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be > 0.")

    now_dt = _now_dt()
    now = now_dt.isoformat()
    lease_expires_at = _iso_after_seconds(lease_seconds)

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        raw = dict(row)
        if raw["status"] not in _CLAIMABLE_STATUSES:
            return None
        if raw["settled_at"] or raw["completed_at"]:
            return None
        if raw["status"] == "pending":
            next_retry_at = _parse_ts(_clean_optional_text(raw.get("next_retry_at")))
            if next_retry_at is not None and next_retry_at > now_dt:
                return None
        if require_authorized_owner and not is_worker_authorized(raw, owner_id):
            return None

        current_owner = _clean_optional_text(raw.get("claim_owner_id"))
        lease_active = _lease_is_active(raw, now_dt)

        if lease_active and current_owner != owner_id:
            return None

        same_owner_reclaim = lease_active and current_owner == owner_id
        attempt_count = _to_non_negative_int(raw.get("attempt_count"), default=0)
        max_attempts = max(1, _to_non_negative_int(raw.get("max_attempts"), default=3))

        if not same_owner_reclaim:
            if attempt_count >= max_attempts:
                return None
            attempt_count += 1
            claim_token = str(uuid.uuid4())
            claimed_at = now
        else:
            claim_token = _clean_optional_text(raw.get("claim_token")) or str(uuid.uuid4())
            claimed_at = _clean_optional_text(raw.get("claimed_at")) or now

        next_status = "running" if raw["status"] == "pending" else raw["status"]

        conn.execute(
            """
            UPDATE jobs
            SET status = ?, claim_owner_id = ?, claim_token = ?, claimed_at = ?,
                lease_expires_at = ?, last_heartbeat_at = ?, attempt_count = ?,
                next_retry_at = NULL, updated_at = ?
            WHERE job_id = ?
            """,
            (
                next_status,
                owner_id,
                claim_token,
                claimed_at,
                lease_expires_at,
                now,
                attempt_count,
                now,
                job_id,
            ),
        )
        event_type = "claim_reclaimed" if current_owner == owner_id else "claim_acquired"
        _insert_claim_event_row(
            conn,
            job_id,
            event_type=event_type,
            claim_owner_id=owner_id,
            claim_token=claim_token,
            lease_started_at=now,
            lease_expires_at=lease_expires_at,
            actor_id=owner_id,
            metadata={
                "status_after": next_status,
                "attempt_count": attempt_count,
            },
            created_at=now,
        )

    return get_job(job_id)


def heartbeat_job_lease(
    job_id: str,
    claim_owner_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    claim_token: str | None = None,
    require_authorized_owner: bool = True,
) -> dict | None:
    """Extend the active lease deadline by ``lease_seconds`` from now.

    The ``claim_token``, when provided, is validated against the stored token
    to prevent a stale worker from extending a lease that has already been
    reclaimed. If the tokens don't match the call is a no-op (returns ``None``).

    Returns the updated job dict on success, or ``None`` if the job is not
    in ``running`` state or the owner/token check fails.
    """
    owner_id = (claim_owner_id or "").strip()
    if not owner_id:
        raise ValueError("claim_owner_id must be a non-empty string.")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be > 0.")

    now_dt = _now_dt()
    now = now_dt.isoformat()
    lease_expires_at = _iso_after_seconds(lease_seconds)

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        raw = dict(row)
        if require_authorized_owner and not is_worker_authorized(raw, owner_id):
            return None
        if raw["status"] not in _ACTIVE_LEASE_STATUSES:
            return None
        if _clean_optional_text(raw.get("claim_owner_id")) != owner_id:
            return None
        if claim_token is not None and _clean_optional_text(raw.get("claim_token")) != claim_token:
            return None

        existing_expiry = _parse_ts(_clean_optional_text(raw.get("lease_expires_at")))
        if existing_expiry is None or existing_expiry <= now_dt:
            return None

        result = conn.execute(
            """
            UPDATE jobs
            SET lease_expires_at = CASE
                    WHEN lease_expires_at > ? THEN lease_expires_at
                    ELSE ?
                END,
                last_heartbeat_at = ?,
                updated_at = ?
            WHERE job_id = ?
              AND status IN ('running', 'awaiting_clarification')
              AND claim_owner_id = ?
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at > ?
            """,
            (
                lease_expires_at,
                lease_expires_at,
                now,
                now,
                job_id,
                owner_id,
                now,
            ),
        )
        if result.rowcount == 0:
            return None

        new_expiry = _parse_ts(lease_expires_at)
        if existing_expiry and new_expiry and existing_expiry > new_expiry:
            effective_expiry = existing_expiry.isoformat()
        else:
            effective_expiry = lease_expires_at
        _insert_claim_event_row(
            conn,
            job_id,
            event_type="claim_heartbeat",
            claim_owner_id=owner_id,
            claim_token=_clean_optional_text(raw.get("claim_token")),
            lease_started_at=now,
            lease_expires_at=effective_expiry,
            actor_id=owner_id,
            metadata={"lease_seconds": lease_seconds},
            created_at=now,
        )

    return get_job(job_id)


def release_job_claim(
    job_id: str,
    claim_owner_id: str,
    claim_token: str | None = None,
    require_authorized_owner: bool = True,
) -> dict | None:
    """Explicitly release a running job back to ``pending`` before the lease expires.

    Workers call this when they determine they cannot complete the job (e.g.
    they lack the required capabilities). This allows the job to be re-claimed
    immediately without waiting for the sweeper's lease-expiry cycle.

    Returns the updated job dict on success, or ``None`` if the ownership or
    token check fails.
    """
    owner_id = (claim_owner_id or "").strip()
    if not owner_id:
        raise ValueError("claim_owner_id must be a non-empty string.")

    now = _now()

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        raw = dict(row)
        if require_authorized_owner and not is_worker_authorized(raw, owner_id):
            return None
        if _clean_optional_text(raw.get("claim_owner_id")) != owner_id:
            return None
        if claim_token is not None and _clean_optional_text(raw.get("claim_token")) != claim_token:
            return None

        previous_claim_token = _clean_optional_text(raw.get("claim_token"))
        previous_lease_expires_at = _clean_optional_text(raw.get("lease_expires_at"))
        conn.execute(
            """
            UPDATE jobs
            SET claim_owner_id = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                updated_at = ?
            WHERE job_id = ?
            """,
            (now, job_id),
        )
        _insert_claim_event_row(
            conn,
            job_id,
            event_type="claim_released",
            claim_owner_id=owner_id,
            claim_token=previous_claim_token,
            lease_started_at=now,
            lease_expires_at=previous_lease_expires_at,
            actor_id=owner_id,
            metadata={},
            created_at=now,
        )

    return get_job(job_id)


def schedule_job_retry(
    job_id: str,
    retry_delay_seconds: int,
    error_message: str | None = None,
    claim_owner_id: str | None = None,
    claim_token: str | None = None,
    require_authorized_owner: bool = True,
) -> dict | None:
    """Re-queue a job as pending after lease expiry, incrementing retry_count up to max_attempts.

    Returns the updated job dict, or None if the job is already settled or at max attempts.
    """
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds must be >= 0.")

    owner_id = _clean_optional_text(claim_owner_id)
    now_dt = _now_dt()
    now = now_dt.isoformat()

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        raw = dict(row)
        if raw["settled_at"]:
            return None
        if raw["completed_at"]:
            return None

        current_claim_owner = _clean_optional_text(raw.get("claim_owner_id"))
        if owner_id is not None:
            if require_authorized_owner and not is_worker_authorized(raw, owner_id):
                return None
            if current_claim_owner != owner_id:
                return None
        if claim_token is not None and _clean_optional_text(raw.get("claim_token")) != claim_token:
            return None

        attempt_count = _to_non_negative_int(raw.get("attempt_count"), default=0)
        max_attempts = max(1, _to_non_negative_int(raw.get("max_attempts"), default=3))
        retry_count = _to_non_negative_int(raw.get("retry_count"), default=0) + 1

        can_retry = attempt_count < max_attempts
        next_status = "pending" if can_retry else "failed"
        next_retry_at = (now_dt + timedelta(seconds=retry_delay_seconds)).isoformat() if can_retry else None
        completed_at = None if can_retry else (_clean_optional_text(raw.get("completed_at")) or now)
        next_error = error_message if error_message is not None else raw.get("error_message")

        conn.execute(
            """
            UPDATE jobs
            SET status = ?,
                error_message = ?,
                updated_at = ?,
                completed_at = ?,
                claim_owner_id = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                retry_count = ?,
                next_retry_at = ?,
                last_retry_at = ?
            WHERE job_id = ?
            """,
            (
                next_status,
                next_error,
                now,
                completed_at,
                retry_count,
                next_retry_at,
                now,
                job_id,
            ),
        )

    return get_job(job_id)


def mark_job_timeout(
    job_id: str,
    retry_delay_seconds: int = 0,
    error_message: str = "Job lease expired before completion.",
    allow_retry: bool = True,
) -> dict | None:
    """Mark a job failed due to lease timeout; used by the sweeper.

    If ``allow_retry`` is True and attempts remain, schedules a retry instead of failing.
    Returns the updated job dict, or None if the job is not in an active lease status.
    """
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds must be >= 0.")

    now_dt = _now_dt()
    now = now_dt.isoformat()

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        raw = dict(row)
        if raw["settled_at"]:
            return None
        if raw["status"] not in _ACTIVE_LEASE_STATUSES:
            return None
        if not _lease_is_expired(raw, now_dt):
            return None

        previous_claim_owner_id = _clean_optional_text(raw.get("claim_owner_id"))
        previous_claim_token = _clean_optional_text(raw.get("claim_token"))
        previous_lease_expires_at = _clean_optional_text(raw.get("lease_expires_at"))
        attempt_count = _to_non_negative_int(raw.get("attempt_count"), default=0)
        max_attempts = max(1, _to_non_negative_int(raw.get("max_attempts"), default=3))
        retry_count = _to_non_negative_int(raw.get("retry_count"), default=0) + 1
        timeout_count = _to_non_negative_int(raw.get("timeout_count"), default=0) + 1

        can_retry = allow_retry and attempt_count < max_attempts
        next_status = "pending" if can_retry else "failed"
        next_retry_at = (now_dt + timedelta(seconds=retry_delay_seconds)).isoformat() if can_retry else None
        completed_at = None if can_retry else (_clean_optional_text(raw.get("completed_at")) or now)

        conn.execute(
            """
            UPDATE jobs
            SET status = ?,
                error_message = ?,
                updated_at = ?,
                completed_at = ?,
                claim_owner_id = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                retry_count = ?,
                next_retry_at = ?,
                last_retry_at = ?,
                timeout_count = ?,
                last_timeout_at = ?
            WHERE job_id = ?
            """,
            (
                next_status,
                error_message,
                now,
                completed_at,
                retry_count,
                next_retry_at,
                now,
                timeout_count,
                now,
                job_id,
            ),
        )
        _insert_claim_event_row(
            conn,
            job_id,
            event_type="claim_timed_out",
            claim_owner_id=previous_claim_owner_id,
            claim_token=previous_claim_token,
            lease_started_at=now,
            lease_expires_at=previous_lease_expires_at,
            metadata={
                "status_after": next_status,
                "retry_count": retry_count,
                "timeout_count": timeout_count,
                "allow_retry": bool(allow_retry),
            },
            created_at=now,
        )

    return get_job(job_id)


def list_jobs_due_for_retry(limit: int = 100, now: str | None = None) -> list:
    """Sweeper query: return pending jobs past their ``next_retry_at`` deadline with attempts remaining."""
    limit = min(max(1, limit), 200)
    now_iso = now or _now()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'pending'
              AND completed_at IS NULL
              AND settled_at IS NULL
              AND next_retry_at IS NOT NULL
              AND next_retry_at <= ?
              AND retry_count < max_attempts
            ORDER BY next_retry_at ASC, created_at ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_retry_ready(job_id: str, now: str | None = None) -> dict | None:
    """
    Clear retry scheduling + lease claim fields so a pending retry becomes claimable.
    Returns the updated job, or None if the row was not due for retry.
    """
    now_iso = now or _now()
    with _conn() as conn:
        result = conn.execute(
            """
            UPDATE jobs
            SET next_retry_at = NULL,
                last_retry_at = NULL,
                claim_owner_id = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                updated_at = ?
            WHERE job_id = ?
              AND status = 'pending'
              AND next_retry_at IS NOT NULL
              AND next_retry_at <= ?
            """,
            (now_iso, job_id, now_iso),
        )
    if result.rowcount == 0:
        return None
    return get_job(job_id)


def list_jobs_with_expired_leases(limit: int = 100, now: str | None = None) -> list:
    """Sweeper query: return running or awaiting_clarification jobs past their ``lease_expires_at``."""
    limit = min(max(1, limit), 200)
    now_iso = now or _now()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status IN ('running', 'awaiting_clarification')
              AND completed_at IS NULL
              AND settled_at IS NULL
              AND claim_owner_id IS NOT NULL
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            ORDER BY lease_expires_at ASC, created_at ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_jobs_with_expired_clarification_deadline(limit: int = 100, now: str | None = None) -> list:
    """Sweeper query: return awaiting_clarification jobs past their ``clarification_deadline_at``."""
    limit = min(max(1, limit), 200)
    now_iso = now or _now()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'awaiting_clarification'
              AND completed_at IS NULL
              AND settled_at IS NULL
              AND clarification_deadline_at IS NOT NULL
              AND clarification_deadline_at <= ?
            ORDER BY clarification_deadline_at ASC, created_at ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_jobs_past_sla(sla_seconds: int, limit: int = 100, now: str | None = None) -> list:
    """Sweeper query: return pending/running/awaiting_clarification jobs older than ``sla_seconds``."""
    if sla_seconds <= 0:
        raise ValueError("sla_seconds must be > 0.")
    limit = min(max(1, limit), 200)
    now_dt = _parse_ts(now or _now()) or _now_dt()
    threshold = (now_dt - timedelta(seconds=sla_seconds)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status IN ('pending', 'running', 'awaiting_clarification')
              AND completed_at IS NULL
              AND settled_at IS NULL
              AND created_at <= ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (threshold, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_jobs_with_expired_output_verification(limit: int = 100, now: str | None = None) -> list:
    """Sweeper query: return complete jobs whose output-verification window has elapsed without a decision."""
    limit = min(max(1, limit), 200)
    now_iso = now or _now()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'complete'
              AND completed_at IS NOT NULL
              AND settled_at IS NULL
              AND output_verification_status = 'pending'
              AND output_verification_deadline_at IS NOT NULL
              AND output_verification_deadline_at <= ?
            ORDER BY output_verification_deadline_at ASC, created_at ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_completed_jobs_pending_settlement(limit: int = 100) -> list:
    """Return complete jobs that have not yet been settled (settled_at IS NULL)."""
    limit = min(max(1, limit), 500)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'complete'
              AND completed_at IS NOT NULL
              AND settled_at IS NULL
            ORDER BY completed_at ASC, created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_job_status(
    job_id: str,
    status: str,
    output_payload: dict | None = None,
    error_message: str | None = None,
    completed: bool = False,
    *,
    output_signature: str | None = None,
    output_signature_alg: str | None = None,
    output_signed_by_did: str | None = None,
    output_signed_at: str | None = None,
) -> dict | None:
    """Low-level status transition; clears claim/lease fields on completion.

    Validates ``status`` against VALID_STATUSES. When ``completed=True``, stamps
    ``completed_at`` and clears lease/claim columns. Returns the updated job or None.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    now = _now()
    clear_claim = 1 if completed else 0
    clear_retry_schedule = 1 if status != "pending" else 0
    completed_flag = 1 if completed else 0
    has_signature = 1 if output_signature else 0

    with _conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, output_payload = ?, error_message = ?,
                updated_at = ?, completed_at = COALESCE(completed_at, ?),
                next_retry_at = CASE WHEN ? = 1 THEN NULL ELSE next_retry_at END,
                claim_owner_id = CASE WHEN ? = 1 THEN NULL ELSE claim_owner_id END,
                claim_token = CASE WHEN ? = 1 THEN NULL ELSE claim_token END,
                lease_expires_at = CASE WHEN ? = 1 THEN NULL ELSE lease_expires_at END,
                last_heartbeat_at = CASE WHEN ? = 1 THEN NULL ELSE last_heartbeat_at END,
                clarification_requested_at = CASE
                    WHEN ? = 1 OR ? != 'awaiting_clarification' THEN NULL
                    ELSE clarification_requested_at
                END,
                clarification_deadline_at = CASE
                    WHEN ? = 1 OR ? != 'awaiting_clarification' THEN NULL
                    ELSE clarification_deadline_at
                END,
                output_signature      = CASE WHEN ? = 1 THEN ? ELSE output_signature END,
                output_signature_alg  = CASE WHEN ? = 1 THEN ? ELSE output_signature_alg END,
                output_signed_by_did  = CASE WHEN ? = 1 THEN ? ELSE output_signed_by_did END,
                output_signed_at      = CASE WHEN ? = 1 THEN ? ELSE output_signed_at END
            WHERE job_id = ? AND (? = 0 OR completed_at IS NULL)
            """,
            (
                status,
                json.dumps(output_payload) if output_payload is not None else None,
                error_message,
                now,
                now if completed else None,
                clear_retry_schedule,
                clear_claim,
                clear_claim,
                clear_claim,
                clear_claim,
                clear_claim,
                status,
                clear_claim,
                status,
                has_signature, output_signature,
                has_signature, output_signature_alg,
                has_signature, output_signed_by_did,
                has_signature, output_signed_at,
                job_id,
                completed_flag,
            ),
        )
    return get_job(job_id)


def mark_settled(job_id: str) -> bool:
    """Set ``settled_at`` timestamp on a completed job. Returns True if the row was updated."""
    now = _now()
    with _conn() as conn:
        result = conn.execute(
            """
            UPDATE jobs
            SET settled_at = ?
            WHERE job_id = ? AND settled_at IS NULL
            """,
            (now, job_id),
        )
    return result.rowcount > 0


def initialize_output_verification_state(job_id: str) -> dict | None:
    """Set up the output-verification window on job completion.

    Only activates if the job has a non-zero ``output_verification_window_seconds`` and is complete.
    Sets ``output_verification_status`` to 'pending' and records the deadline.
    """
    now = _now()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        raw = dict(row)
        if raw.get("status") != "complete" or _clean_optional_text(raw.get("completed_at")) is None:
            return _row_to_dict(row)

        window_seconds = _to_non_negative_int(
            raw.get("output_verification_window_seconds"),
            default=0,
        )
        status = _normalize_output_verification_status(raw.get("output_verification_status"))
        completed_at_dt = _parse_ts(_clean_optional_text(raw.get("completed_at")))
        deadline = (
            (completed_at_dt + timedelta(seconds=window_seconds)).isoformat()
            if completed_at_dt is not None and window_seconds > 0
            else None
        )

        if window_seconds <= 0:
            if (
                status == "not_required"
                and _clean_optional_text(raw.get("output_verification_deadline_at")) is None
                and _clean_optional_text(raw.get("output_verification_decided_at")) is None
                and _clean_optional_text(raw.get("output_verification_decision_owner_id")) is None
                and _clean_optional_text(raw.get("output_verification_reason")) is None
            ):
                return _row_to_dict(row)
            conn.execute(
                """
                UPDATE jobs
                SET output_verification_status = 'not_required',
                    output_verification_deadline_at = NULL,
                    output_verification_decided_at = NULL,
                    output_verification_decision_owner_id = NULL,
                    output_verification_reason = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (now, job_id),
            )
            return get_job(job_id)

        if status == "pending" and _clean_optional_text(raw.get("output_verification_deadline_at")):
            return _row_to_dict(row)
        if status in {"accepted", "rejected", "expired"}:
            return _row_to_dict(row)

        conn.execute(
            """
            UPDATE jobs
            SET output_verification_status = 'pending',
                output_verification_deadline_at = ?,
                output_verification_decided_at = NULL,
                output_verification_decision_owner_id = NULL,
                output_verification_reason = NULL,
                updated_at = ?
            WHERE job_id = ?
            """,
            (deadline, now, job_id),
        )
    return get_job(job_id)


def set_output_verification_decision(
    job_id: str,
    *,
    decision: str,
    decision_owner_id: str,
    reason: str | None = None,
) -> dict | None:
    """Record the caller's accept/reject decision during the output-verification window.

    ``decision`` must be 'accept' or 'reject'. Returns None if the job is not in 'pending'
    verification state or if the job does not exist.
    """
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"accept", "reject"}:
        raise ValueError("decision must be 'accept' or 'reject'.")
    next_status = "accepted" if normalized_decision == "accept" else "rejected"
    now = _now()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        raw = dict(row)
        current = _normalize_output_verification_status(raw.get("output_verification_status"))
        if current == next_status:
            return _row_to_dict(row)
        if current != "pending":
            return None
        conn.execute(
            """
            UPDATE jobs
            SET output_verification_status = ?,
                output_verification_decided_at = ?,
                output_verification_decision_owner_id = ?,
                output_verification_reason = ?,
                updated_at = ?
            WHERE job_id = ?
            """,
            (
                next_status,
                now,
                _clean_optional_text(decision_owner_id),
                _clean_optional_text(reason),
                now,
                job_id,
            ),
        )
    return get_job(job_id)


def mark_output_verification_expired(job_id: str, *, decision_owner_id: str = "system:verification-expiry") -> dict | None:
    """Called by the sweeper when the output-verification window expires without a caller decision.

    Sets ``output_verification_status`` to 'expired'. Returns None if the window has not expired
    or verification is not in 'pending' state.
    """
    now = _now()
    with _conn() as conn:
        result = conn.execute(
            """
            UPDATE jobs
            SET output_verification_status = 'expired',
                output_verification_decided_at = COALESCE(output_verification_decided_at, ?),
                output_verification_decision_owner_id = COALESCE(output_verification_decision_owner_id, ?),
                output_verification_reason = COALESCE(output_verification_reason, 'Verification window expired without caller decision.'),
                updated_at = ?
            WHERE job_id = ?
              AND output_verification_status = 'pending'
              AND output_verification_deadline_at IS NOT NULL
              AND output_verification_deadline_at <= ?
            """,
            (
                now,
                _clean_optional_text(decision_owner_id),
                now,
                job_id,
                now,
            ),
        )
    if result.rowcount == 0:
        return None
    return get_job(job_id)


def _message_correlation_exists_conn(
    conn: sqlite3.Connection,
    job_id: str,
    correlation_id: str,
    msg_type: str | None = None,
) -> bool:
    correlation = _clean_optional_text(correlation_id)
    if correlation is None:
        return False
    if msg_type is not None:
        row = conn.execute(
            """
            SELECT 1
            FROM job_messages
            WHERE job_id = ? AND correlation_id = ? AND type = ?
            LIMIT 1
            """,
            (job_id, correlation, msg_type),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1
            FROM job_messages
            WHERE job_id = ? AND correlation_id = ?
            LIMIT 1
            """,
            (job_id, correlation),
        ).fetchone()
    return row is not None


def message_correlation_exists(
    job_id: str,
    correlation_id: str,
    msg_type: str | None = None,
) -> bool:
    """Idempotency guard: return True if a message with this ``correlation_id`` already exists for the job."""
    with _conn() as conn:
        return _message_correlation_exists_conn(
            conn,
            job_id=job_id,
            correlation_id=correlation_id,
            msg_type=msg_type,
        )


def tool_call_correlation_exists(job_id: str, correlation_id: str) -> bool:
    return message_correlation_exists(job_id, correlation_id, msg_type="tool_call")


def _resolve_message_lease_behavior(raw_type: str, canonical_type: str) -> str | None:
    if canonical_type in MESSAGE_TYPE_LEASE_BEHAVIOR:
        return MESSAGE_TYPE_LEASE_BEHAVIOR[canonical_type]
    return _LEGACY_MESSAGE_TYPE_LEASE_BEHAVIOR.get(raw_type)

