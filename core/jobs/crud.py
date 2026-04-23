"""Job CRUD: creation, listings, and authorisation lookups.

Everything here builds on the primitives in ``core.jobs.db`` (schema,
connection, JSON helpers). Functions in this module:

- ``create_job`` — insert a new job row and the accompanying ``pending`` claim
  event. Charging happens in the server route before this is called.
- ``get_job`` / ``get_jobs_by_caller`` / ``list_jobs_for_agent`` — paginated
  read helpers with stable cursors.
- ``get_job_authorization_context`` and the ``is_worker_authorized`` family —
  resolve whether a given caller/worker/admin may see or mutate a job.

These helpers never touch wallets, the ledger, or dispute state — those are
owned by ``core.payments`` and the server shards, respectively.
"""
from __future__ import annotations

import json
import uuid

from .db import (
    VALID_STATUSES,
    _clean_optional_text,
    _conn,
    _decode_json,
    _iso_after_seconds,
    _msg_to_dict,
    _normalize_clarification_timeout_policy,
    _normalize_fee_bearer_policy,
    _normalize_parent_cascade_policy,
    _now,
    _now_dt,
    _parse_ts,
    _row_to_dict,
    _to_non_negative_int,
)
def create_job(
    agent_id: str,
    caller_owner_id: str,
    caller_wallet_id: str,
    agent_wallet_id: str,
    platform_wallet_id: str,
    price_cents: int,
    charge_tx_id: str,
    input_payload: dict,
    caller_charge_cents: int | None = None,
    platform_fee_pct_at_create: int = 10,
    fee_bearer_policy: str = "caller",
    agent_owner_id: str | None = None,
    max_attempts: int = 3,
    parent_job_id: str | None = None,
    tree_depth: int = 0,
    parent_cascade_policy: str = "detach",
    clarification_timeout_seconds: int | None = None,
    clarification_timeout_policy: str = "fail",
    dispute_window_hours: int = 72,
    judge_agent_id: str | None = None,
    callback_url: str | None = None,
    callback_secret: str | None = None,
    output_verification_window_seconds: int | None = None,
    batch_id: str | None = None,
) -> dict:
    if price_cents < 0:
        raise ValueError("price_cents must be non-negative.")
    parsed_caller_charge_cents = _to_non_negative_int(caller_charge_cents, default=price_cents)
    if parsed_caller_charge_cents <= 0 and price_cents > 0:
        raise ValueError(
            "invalid_charge_amount: caller_charge_cents must be positive when price is non-zero."
        )
    if parsed_caller_charge_cents < price_cents:
        raise ValueError("caller_charge_cents must be >= price_cents.")
    # Hard cap: caller_charge_cents must not exceed price_cents * 2 (room for 100% platform fee)
    # to prevent inflated charges that would produce a negative net payout on partial refund.
    if parsed_caller_charge_cents > max(price_cents * 2, price_cents + 1000):
        raise ValueError(
            "charge_exceeds_listed_price: caller_charge_cents must not exceed 2x price_cents."
        )
    parsed_platform_fee_pct = _to_non_negative_int(platform_fee_pct_at_create, default=10)
    if parsed_platform_fee_pct > 100:
        raise ValueError("platform_fee_pct_at_create must be <= 100.")
    normalized_fee_bearer_policy = _normalize_fee_bearer_policy(fee_bearer_policy)

    parsed_max_attempts = _to_non_negative_int(max_attempts, default=0)
    if parsed_max_attempts < 1:
        raise ValueError("max_attempts must be >= 1.")
    parsed_tree_depth = _to_non_negative_int(tree_depth, default=0)
    normalized_parent_cascade_policy = _normalize_parent_cascade_policy(parent_cascade_policy)
    parsed_clarification_timeout_seconds = _to_non_negative_int(
        clarification_timeout_seconds,
        default=0,
    )
    normalized_clarification_timeout_policy = _normalize_clarification_timeout_policy(
        clarification_timeout_policy
    )
    parsed_dispute_window_hours = _to_non_negative_int(dispute_window_hours, default=0)
    if parsed_dispute_window_hours < 1:
        raise ValueError("dispute_window_hours must be >= 1.")
    parsed_output_verification_window_seconds = _to_non_negative_int(
        output_verification_window_seconds,
        default=0,
    )

    owner_id = (agent_owner_id or f"agent:{agent_id}").strip()
    if not owner_id:
        raise ValueError("agent_owner_id must be a non-empty string.")

    job_id = str(uuid.uuid4())
    now = _now()

    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs
              (job_id, agent_id, agent_owner_id, caller_owner_id, caller_wallet_id,
               agent_wallet_id, platform_wallet_id, status, price_cents, caller_charge_cents,
               platform_fee_pct_at_create, fee_bearer_policy, charge_tx_id,
               input_payload, created_at, updated_at, max_attempts, parent_job_id, tree_depth, parent_cascade_policy,
               clarification_timeout_seconds, clarification_timeout_policy, dispute_window_hours, judge_agent_id,
               callback_url, callback_secret, output_verification_window_seconds, output_verification_status, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                agent_id,
                owner_id,
                caller_owner_id,
                caller_wallet_id,
                agent_wallet_id,
                platform_wallet_id,
                "pending",
                price_cents,
                parsed_caller_charge_cents,
                parsed_platform_fee_pct,
                normalized_fee_bearer_policy,
                charge_tx_id,
                json.dumps(input_payload),
                now,
                now,
                parsed_max_attempts,
                _clean_optional_text(parent_job_id),
                parsed_tree_depth,
                normalized_parent_cascade_policy,
                parsed_clarification_timeout_seconds,
                normalized_clarification_timeout_policy,
                parsed_dispute_window_hours,
                _clean_optional_text(judge_agent_id),
                _clean_optional_text(callback_url),
                _clean_optional_text(callback_secret),
                parsed_output_verification_window_seconds,
                "not_required",
                _clean_optional_text(batch_id),
            ),
        )
    return get_job(job_id)


def list_jobs_for_batch(batch_id: str, caller_owner_id: str) -> list[dict]:
    normalized_batch_id = _clean_optional_text(batch_id)
    normalized_owner_id = _clean_optional_text(caller_owner_id)
    if normalized_batch_id is None or normalized_owner_id is None:
        return []
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE batch_id = ? AND caller_owner_id = ?
            ORDER BY created_at ASC, job_id ASC
            """,
            (normalized_batch_id, normalized_owner_id),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_child_jobs(
    parent_job_id: str,
    *,
    statuses: tuple[str, ...] | None = None,
    limit: int = 200,
) -> list[dict]:
    normalized_parent = _clean_optional_text(parent_job_id)
    if normalized_parent is None:
        return []
    capped_limit = min(max(1, int(limit)), 500)
    params: list = [normalized_parent]
    where_status = ""
    if statuses:
        normalized_statuses = tuple(
            status for status in statuses if isinstance(status, str) and status in VALID_STATUSES
        )
        if normalized_statuses:
            placeholders = ", ".join(["?"] * len(normalized_statuses))
            where_status = f" AND status IN ({placeholders})"
            params.extend(normalized_statuses)
    params.append(capped_limit)
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM jobs
            WHERE parent_job_id = ?
            {where_status}
            ORDER BY created_at ASC, job_id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_job(job_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_job_authorization_context(job_id: str) -> dict | None:
    job = get_job(job_id)
    if job is None:
        return None
    return {
        "job_id": job["job_id"],
        "agent_id": job["agent_id"],
        "agent_owner_id": job["agent_owner_id"],
        "caller_owner_id": job["caller_owner_id"],
        "claim_owner_id": job["claim_owner_id"],
    }


def is_worker_authorized(job: dict, worker_owner_id: str) -> bool:
    expected_owner = (job.get("agent_owner_id") or "").strip()
    candidate = (worker_owner_id or "").strip()
    return bool(expected_owner) and candidate == expected_owner


def is_worker_authorized_for_job(job_id: str, worker_owner_id: str) -> bool:
    job = get_job(job_id)
    if job is None:
        return False
    return is_worker_authorized(job, worker_owner_id)


def list_jobs_for_owner(
    owner_id: str,
    limit: int = 50,
    status: str | None = None,
    before_created_at: str | None = None,
    before_job_id: str | None = None,
) -> list:
    limit = min(max(1, limit), 200)
    where_clauses = ["caller_owner_id = ?"]
    params: list = [owner_id]
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if before_created_at:
        cursor_job_id = before_job_id or "\uffff"
        where_clauses.append("(created_at < ? OR (created_at = ? AND job_id < ?))")
        params.extend([before_created_at, before_created_at, cursor_job_id])
    where_sql = " AND ".join(where_clauses)
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {where_sql}
            ORDER BY created_at DESC, job_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_jobs_for_agent(
    agent_id: str,
    limit: int = 50,
    status: str | None = None,
    before_created_at: str | None = None,
    before_job_id: str | None = None,
) -> list:
    limit = min(max(1, limit), 200)
    where_clauses = ["agent_id = ?"]
    params: list = [agent_id]
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if before_created_at:
        cursor_job_id = before_job_id or "\uffff"
        where_clauses.append("(created_at < ? OR (created_at = ? AND job_id < ?))")
        params.extend([before_created_at, before_created_at, cursor_job_id])
    where_sql = " AND ".join(where_clauses)
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {where_sql}
            ORDER BY created_at DESC, job_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]

