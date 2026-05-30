"""DB layer for the agent_generation_jobs table (migration 0042).

# OWNS: CRUD for agent_generation_jobs rows.
# NOT OWNS: business logic, payment, the agents/hosted_skills tables.
# INVARIANTS:
#   - status ∈ {'queued','running','succeeded','failed'}.
#   - UNIQUE(owner_id, idempotency_key) is enforced at the schema level;
#     create_or_get returns the existing row on collision (idempotent retries).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from core import db as _db
from core.db import get_db_connection

# Allowed status values; anything else raises before hitting the DB.
_VALID_STATUSES = frozenset({"queued", "running", "succeeded", "failed"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    # SQLite Row supports keys()
    return {k: row[k] for k in row.keys()}


def _decode_json(value: Any, fallback: Any) -> Any:
    """Best-effort JSON decode.  Used because SQLite stores JSON as TEXT.

    Why fallback rather than raise: a corrupt cell from a half-written row
    must not 500 the polling endpoint; the row is still useful for status.
    """
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def create_or_get_generation_job(
    *,
    owner_id: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Insert a new generation job, or return the existing one on collision.

    Returns ``(row, created)``. ``created=False`` when the (owner, idempotency)
    pair already exists; the caller must NOT charge the wallet again.
    """
    if not owner_id or not idempotency_key:
        raise ValueError("owner_id and idempotency_key are required.")
    job_id = f"gen_{uuid.uuid4().hex[:24]}"
    now = _utc_now_iso()
    request_json = json.dumps(request_payload, default=str, sort_keys=True)
    with get_db_connection() as _raw_conn, _raw_conn as conn:
        existing = conn.execute(
            "SELECT * FROM agent_generation_jobs"
            " WHERE owner_id = %s AND idempotency_key = %s",
            (owner_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            return _row_to_dict(existing), False
        try:
            conn.execute(
                "INSERT INTO agent_generation_jobs"
                " (generation_job_id, owner_id, idempotency_key, status,"
                "  request_json, iterations, cost_cents, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, 0, 0, %s, %s)",
                (job_id, owner_id, idempotency_key, "queued",
                 request_json, now, now),
            )
        except _db.IntegrityError:
            # Concurrent insert — re-read the row.
            existing = conn.execute(
                "SELECT * FROM agent_generation_jobs"
                " WHERE owner_id = %s AND idempotency_key = %s",
                (owner_id, idempotency_key),
            ).fetchone()
            if existing is None:
                raise
            return _row_to_dict(existing), False
    fresh = get_generation_job(job_id)
    if fresh is None:
        # Genuinely impossible after a successful insert; raise rather than
        # masking the bug with a default dict.
        raise RuntimeError(f"Generation job {job_id} disappeared after insert.")
    return fresh, True


def get_generation_job(generation_job_id: str) -> dict[str, Any] | None:
    """Fetch one job row by id, or None if not found."""
    with get_db_connection() as _raw_conn, _raw_conn as conn:
        row = conn.execute(
            "SELECT * FROM agent_generation_jobs WHERE generation_job_id = %s",
            (generation_job_id,),
        ).fetchone()
    return _row_to_dict(row)


def list_recent_for_owner(owner_id: str, *, since_iso: str) -> list[dict[str, Any]]:
    """Rows for an owner created at or after ``since_iso``.  Used for the
    per-day rate-limit check (count rows in the last 24h)."""
    with get_db_connection() as _raw_conn, _raw_conn as conn:
        rows = conn.execute(
            "SELECT * FROM agent_generation_jobs"
            " WHERE owner_id = %s AND created_at >= %s"
            " ORDER BY created_at DESC",
            (owner_id, since_iso),
        ).fetchall()
    return [_row_to_dict(r) or {} for r in rows]


def update_status(
    generation_job_id: str,
    *,
    status: str,
    iterations: int | None = None,
    cost_cents: int | None = None,
    agent_id: str | None = None,
    charge_tx_id: str | None = None,
    result_payload: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update mutable columns on a generation job row.

    Why a kitchen-sink updater: every terminal transition writes 3-5 columns
    in one shot; splitting into update_succeeded/update_failed/etc would
    duplicate the WHERE clause and the timestamp logic without paying back.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'.")
    fields: list[str] = ["status = %s", "updated_at = %s"]
    params: list[Any] = [status, _utc_now_iso()]
    if iterations is not None:
        fields.append("iterations = %s")
        params.append(int(iterations))
    if cost_cents is not None:
        fields.append("cost_cents = %s")
        params.append(int(cost_cents))
    if agent_id is not None:
        fields.append("agent_id = %s")
        params.append(agent_id)
    if charge_tx_id is not None:
        fields.append("charge_tx_id = %s")
        params.append(charge_tx_id)
    if result_payload is not None:
        fields.append("result_json = %s")
        params.append(json.dumps(result_payload, default=str, sort_keys=True))
    if error_code is not None:
        fields.append("error_code = %s")
        params.append(error_code)
    if error_message is not None:
        fields.append("error_message = %s")
        params.append(error_message)
    params.append(generation_job_id)
    with get_db_connection() as _raw_conn, _raw_conn as conn:
        conn.execute(
            "UPDATE agent_generation_jobs SET "
            + ", ".join(fields)
            + " WHERE generation_job_id = %s",
            tuple(params),
        )


def deserialize_result(row: dict[str, Any]) -> dict[str, Any]:
    """Decode the persisted result JSON, returning {} when absent or bad."""
    return _decode_json(row.get("result_json"), {})
