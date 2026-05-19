# OWNS: job creation, job read helpers, authorization lookups
# NOT OWNS: wallets/ledger (payments/base.py), lease transitions (leases.py),
#           dispute state (disputes.py), typed messages (messaging.py)
#
# INVARIANTS:
# - charging happens in the server route BEFORE create_job is called — never charge inside here
# - authorization checks (is_worker_authorized, get_job_authorization_context) are used by
#   server routes but the authoritative check is in the route layer, not here
#
# DECISIONS:
# - pagination uses stable cursors (created_at + job_id) rather than OFFSET to avoid skips
#   under concurrent inserts — don't switch to OFFSET-based pagination
from __future__ import annotations

import json
import uuid
from typing import Any

from core.functional import Err, pipe, validate

from .db import (
    VALID_STATUSES,
    _clean_optional_text,
    _conn,
    _normalize_clarification_timeout_policy,
    _normalize_fee_bearer_policy,
    _normalize_parent_cascade_policy,
    _now,
    _row_to_dict,
    _to_non_negative_int,
)

# Default values for job creation — named here so callers and the DB layer
# both reference the same source of truth.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_DISPUTE_WINDOW_HOURS = 72
DEFAULT_PLATFORM_FEE_PCT = 10
# Max caller_charge is 2x price (covers 100% fee) plus a $10 buffer for rounding.
_MAX_CALLER_CHARGE_BUFFER_CENTS = 1000

# B15, 2026-05-19: default time-to-claim. After this elapses without a
# worker picking up the job, the sweeper transitions it to `failed` with
# error_message=agent.no_workers_claimed and refunds the wallet hold.
_DEFAULT_CLAIM_DEADLINE_SECONDS = 1800  # 30 minutes


def _compute_claim_deadline_iso(now_iso: str) -> str:
    """Pure: return ISO-8601 string for `now + AZTEA_JOB_CLAIM_DEADLINE_SECONDS`.

    Env-tunable so high-latency external agents can ask for a longer
    window. Falls back to the 30-minute default on bad input.
    """
    import os
    from datetime import datetime, timedelta, timezone

    raw = os.environ.get("AZTEA_JOB_CLAIM_DEADLINE_SECONDS")
    try:
        seconds = max(60, int(raw)) if raw else _DEFAULT_CLAIM_DEADLINE_SECONDS
    except (TypeError, ValueError):
        seconds = _DEFAULT_CLAIM_DEADLINE_SECONDS
    try:
        base = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        base = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat()

_CREATE_JOB_INSERT_SQL = """
    INSERT INTO jobs
      (job_id, agent_id, agent_owner_id, caller_owner_id, caller_wallet_id,
       agent_wallet_id, platform_wallet_id, status, price_cents, caller_charge_cents,
       platform_fee_pct_at_create, fee_bearer_policy, client_id, charge_tx_id,
       input_payload, created_at, updated_at, max_attempts, parent_job_id, tree_depth,
       parent_cascade_policy, clarification_timeout_seconds, clarification_timeout_policy,
       dispute_window_hours, judge_agent_id, callback_url, callback_secret,
       output_verification_window_seconds, output_verification_status, batch_id, origin)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# Allowed values for jobs.origin. NULL is treated as "unknown / pre-migration"
# by readers; the backfill script promotes NULL to 'direct' for rows with no
# pipeline/compare/recipe/watcher join match. See migration 0049.
_ALLOWED_JOB_ORIGINS: frozenset[str] = frozenset({
    "direct", "auto_hire", "pipeline", "compare", "recipe", "watcher",
})


def _validate_origin(origin: str | None) -> str | None:
    """Pure: accept None or an allowed taxonomy value. Raise on unknowns so a typo fails loud."""
    if origin is None:
        return None
    if origin not in _ALLOWED_JOB_ORIGINS:
        raise ValueError(
            f"invalid job origin {origin!r}; allowed values: "
            f"{sorted(_ALLOWED_JOB_ORIGINS)}"
        )
    return origin


def _validate_create_job_pricing(
    price_cents: int, parsed_caller_charge_cents: int,
) -> None:
    """Pure: enforce price/charge invariants — non-negative, mutually consistent, capped.

    Why: rejects malformed money inputs at the boundary so the ledger
    never sees a charge that produces a negative net payout.
    """
    _price_check = (
        validate(price_cents, lambda p: p >= 0, "price_cents must be non-negative.")
        .and_then(lambda p: validate(
            p,
            lambda x: not (parsed_caller_charge_cents <= 0 and x > 0),
            "invalid_charge_amount: caller_charge_cents must be positive when price is non-zero.",
        ))
        .and_then(lambda p: validate(
            p,
            lambda x: parsed_caller_charge_cents >= x,
            "caller_charge_cents must be >= price_cents.",
        ))
        .and_then(lambda p: validate(
            p,
            lambda x: parsed_caller_charge_cents
            <= max(x * 2, x + _MAX_CALLER_CHARGE_BUFFER_CENTS),
            "charge_exceeds_listed_price: caller_charge_cents must not exceed 2x price_cents.",
        ))
    )
    _price_check.raise_on_err()


def _normalize_create_job_inputs(
    *, caller_charge_cents: int | None, price_cents: int,
    platform_fee_pct_at_create: int, fee_bearer_policy: str,
    max_attempts: int, tree_depth: int, parent_cascade_policy: str,
    clarification_timeout_seconds: int | None, clarification_timeout_policy: str,
    dispute_window_hours: int, output_verification_window_seconds: int | None,
    agent_owner_id: str | None, agent_id: str,
) -> dict[str, Any]:
    """Pure: validate + coerce every job-shape parameter; raises ValueError on bad input."""
    parsed_caller_charge_cents = _to_non_negative_int(
        caller_charge_cents, default=price_cents,
    )
    _validate_create_job_pricing(price_cents, parsed_caller_charge_cents)
    parsed_platform_fee_pct = _to_non_negative_int(
        platform_fee_pct_at_create, default=DEFAULT_PLATFORM_FEE_PCT,
    )
    if parsed_platform_fee_pct > 100:
        raise ValueError("platform_fee_pct_at_create must be <= 100.")
    parsed_max_attempts = _to_non_negative_int(max_attempts, default=0)
    if parsed_max_attempts < 1:
        raise ValueError("max_attempts must be >= 1.")
    parsed_dispute_window_hours = _to_non_negative_int(dispute_window_hours, default=0)
    if parsed_dispute_window_hours < 1:
        raise ValueError("dispute_window_hours must be >= 1.")
    owner_id = (agent_owner_id or f"agent:{agent_id}").strip()
    if not owner_id:
        raise ValueError("agent_owner_id must be a non-empty string.")
    return {
        "parsed_caller_charge_cents": parsed_caller_charge_cents,
        "parsed_platform_fee_pct": parsed_platform_fee_pct,
        "normalized_fee_bearer_policy": _normalize_fee_bearer_policy(fee_bearer_policy),
        "parsed_max_attempts": parsed_max_attempts,
        "parsed_tree_depth": _to_non_negative_int(tree_depth, default=0),
        "normalized_parent_cascade_policy": _normalize_parent_cascade_policy(
            parent_cascade_policy,
        ),
        "parsed_clarification_timeout_seconds": _to_non_negative_int(
            clarification_timeout_seconds, default=0,
        ),
        "normalized_clarification_timeout_policy": _normalize_clarification_timeout_policy(
            clarification_timeout_policy,
        ),
        "parsed_dispute_window_hours": parsed_dispute_window_hours,
        "parsed_output_verification_window_seconds": _to_non_negative_int(
            output_verification_window_seconds, default=0,
        ),
        "owner_id": owner_id,
    }


def _build_create_job_insert_params(
    *, job_id: str, agent_id: str, caller_owner_id: str, caller_wallet_id: str,
    agent_wallet_id: str, platform_wallet_id: str, price_cents: int,
    charge_tx_id: str, input_payload: dict, parent_job_id: str | None,
    client_id: str | None, judge_agent_id: str | None,
    callback_url: str | None, callback_secret: str | None,
    batch_id: str | None, origin: str | None, now: str,
    normalised: dict[str, Any],
) -> tuple:
    """Pure: positional args for ``_CREATE_JOB_INSERT_SQL`` placeholders.

    Why: ``output_verification_status`` is set to ``armed`` when a window
    is configured so the contract is visible from the moment the job is
    queued; ``arm_output_verification_window`` later transitions it to
    ``pending`` after completion.
    """
    has_verification_window = (
        normalised["parsed_output_verification_window_seconds"] > 0
    )
    return (
        job_id,
        agent_id,
        normalised["owner_id"],
        caller_owner_id,
        caller_wallet_id,
        agent_wallet_id,
        platform_wallet_id,
        "pending",
        price_cents,
        normalised["parsed_caller_charge_cents"],
        normalised["parsed_platform_fee_pct"],
        normalised["normalized_fee_bearer_policy"],
        _clean_optional_text(client_id),
        charge_tx_id,
        json.dumps(input_payload),
        now,
        now,
        normalised["parsed_max_attempts"],
        _clean_optional_text(parent_job_id),
        normalised["parsed_tree_depth"],
        normalised["normalized_parent_cascade_policy"],
        normalised["parsed_clarification_timeout_seconds"],
        normalised["normalized_clarification_timeout_policy"],
        normalised["parsed_dispute_window_hours"],
        _clean_optional_text(judge_agent_id),
        _clean_optional_text(callback_url),
        _clean_optional_text(callback_secret),
        normalised["parsed_output_verification_window_seconds"],
        "armed" if has_verification_window else "not_required",
        _clean_optional_text(batch_id),
        origin,
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
    platform_fee_pct_at_create: int = DEFAULT_PLATFORM_FEE_PCT,
    fee_bearer_policy: str = "caller",
    client_id: str | None = None,
    agent_owner_id: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    parent_job_id: str | None = None,
    tree_depth: int = 0,
    parent_cascade_policy: str = "detach",
    clarification_timeout_seconds: int | None = None,
    clarification_timeout_policy: str = "fail",
    dispute_window_hours: int = DEFAULT_DISPUTE_WINDOW_HOURS,
    judge_agent_id: str | None = None,
    callback_url: str | None = None,
    callback_secret: str | None = None,
    output_verification_window_seconds: int | None = None,
    batch_id: str | None = None,
    origin: str | None = None,
    budget_cents: int | None = None,
) -> dict:
    """Side-effect: insert a new job row and its initial ``pending`` claim event.

    Why: the caller wallet must already have been debited (``charge_tx_id``) before
    calling this — job creation records the charge but does NOT perform it. All
    integer amounts must be in cents; floats are rejected. ``ValueError`` on bad
    money amounts. ``origin`` (when set) tags the row with the surface that
    created it; see ``_ALLOWED_JOB_ORIGINS`` and migration 0049.
    """
    normalised_origin = _validate_origin(origin)
    normalised = _normalize_create_job_inputs(
        caller_charge_cents=caller_charge_cents, price_cents=price_cents,
        platform_fee_pct_at_create=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy, max_attempts=max_attempts,
        tree_depth=tree_depth, parent_cascade_policy=parent_cascade_policy,
        clarification_timeout_seconds=clarification_timeout_seconds,
        clarification_timeout_policy=clarification_timeout_policy,
        dispute_window_hours=dispute_window_hours,
        output_verification_window_seconds=output_verification_window_seconds,
        agent_owner_id=agent_owner_id, agent_id=agent_id,
    )
    job_id = str(uuid.uuid4())
    now = _now()
    params = _build_create_job_insert_params(
        job_id=job_id, agent_id=agent_id, caller_owner_id=caller_owner_id,
        caller_wallet_id=caller_wallet_id, agent_wallet_id=agent_wallet_id,
        platform_wallet_id=platform_wallet_id, price_cents=price_cents,
        charge_tx_id=charge_tx_id, input_payload=input_payload,
        parent_job_id=parent_job_id, client_id=client_id,
        judge_agent_id=judge_agent_id, callback_url=callback_url,
        callback_secret=callback_secret, batch_id=batch_id,
        origin=normalised_origin, now=now,
        normalised=normalised,
    )
    # B15, 2026-05-19: pin a claim deadline at INSERT so the sweeper can
    # auto-fail jobs whose workers never show up. Default 30 min; env-
    # tunable via AZTEA_JOB_CLAIM_DEADLINE_SECONDS so high-latency
    # external agents can ask for a longer window.
    claim_deadline_iso = _compute_claim_deadline_iso(now)
    with _conn() as conn:
        conn.execute(_CREATE_JOB_INSERT_SQL, params)
        # Separate UPDATE keeps the INSERT signature stable. Column added
        # in migration 0062.
        conn.execute(
            "UPDATE jobs SET claim_deadline_at = %s WHERE job_id = %s",
            (claim_deadline_iso, job_id),
        )
        # F8 (red-team 2026-05-19): persist the caller-submitted soft cap
        # so JobResponse echoes back the value the caller submitted.
        # ``budget_cents`` and ``max_price_cents`` are aliases at the
        # request layer; whichever is non-null (or the MIN of both) is
        # stored here. Migration 0063 added the column.
        if budget_cents is not None:
            conn.execute(
                "UPDATE jobs SET budget_cents = %s WHERE job_id = %s",
                (int(budget_cents), job_id),
            )
    return get_job(job_id)


def list_jobs_for_batch(batch_id: str, caller_owner_id: str) -> list[dict]:
    """Return all jobs belonging to ``batch_id``, scoped to the given ``caller_owner_id``."""
    normalized_batch_id = _clean_optional_text(batch_id)
    normalized_owner_id = _clean_optional_text(caller_owner_id)
    if normalized_batch_id is None or normalized_owner_id is None:
        return []
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE batch_id = %s AND caller_owner_id = %s
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
    """Return child jobs of ``parent_job_id``, optionally filtered by ``statuses``. Max 500."""
    normalized_parent = _clean_optional_text(parent_job_id)
    if normalized_parent is None:
        return []
    capped_limit = min(max(1, int(limit)), 500)
    params: list = [normalized_parent]
    where_status = ""
    if statuses:
        normalized_statuses = tuple(
            status
            for status in statuses
            if isinstance(status, str) and status in VALID_STATUSES
        )
        if normalized_statuses:
            placeholders = ", ".join(["%s"] * len(normalized_statuses))
            where_status = f" AND status IN ({placeholders})"
            params.extend(normalized_statuses)
    params.append(capped_limit)
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM jobs
            WHERE parent_job_id = %s
            {where_status}
            ORDER BY created_at ASC, job_id ASC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_job(job_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_job_authorization_context(job_id: str) -> dict | None:
    """Return the minimal ownership fields needed for auth checks: job_id, agent_id, agent_owner_id, caller_owner_id, claim_owner_id."""
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
    """Paginated job list for a caller (buyer view); keyset-paginated on ``before_created_at``/``before_job_id``."""
    limit = min(max(1, limit), 200)
    where_clauses = ["caller_owner_id = %s"]
    params: list = [owner_id]
    if status:
        where_clauses.append("status = %s")
        params.append(status)
    if before_created_at:
        cursor_job_id = before_job_id or "\uffff"
        where_clauses.append("(created_at < %s OR (created_at = %s AND job_id < %s))")
        params.extend([before_created_at, before_created_at, cursor_job_id])
    where_sql = " AND ".join(where_clauses)
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {where_sql}
            ORDER BY created_at DESC, job_id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_pending_jobs(limit: int = 200, agent_ids: list | None = None) -> list:
    """All pending jobs across the platform, oldest first, capped at ``limit``.

    Used by the builtin-job worker pool to drain the queue with a single DB
    round-trip rather than N per-agent queries. Returns oldest-first so FIFO
    fairness is preserved across agents.
    """
    limit = min(max(1, int(limit)), 5000)
    params: list = ["pending"]
    where = "status = %s"
    if agent_ids:
        ids = [str(a) for a in agent_ids if a]
        if ids:
            placeholders = ", ".join(["%s"] * len(ids))
            where += f" AND agent_id IN ({placeholders})"
            params.extend(ids)
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {where}
            ORDER BY created_at ASC, job_id ASC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_pending_jobs(agent_ids: list | None = None) -> int:
    """Cheap COUNT(*) for queue depth display in batch_status traces."""
    params: list = ["pending"]
    where = "status = %s"
    if agent_ids:
        ids = [str(a) for a in agent_ids if a]
        if ids:
            placeholders = ", ".join(["%s"] * len(ids))
            where += f" AND agent_id IN ({placeholders})"
            params.extend(ids)
    with _conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM jobs WHERE {where}",
            tuple(params),
        ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0] if not isinstance(row, dict) else row.get("n") or 0)
    except (TypeError, ValueError):
        return 0


def list_jobs_for_agent(
    agent_id: str,
    limit: int = 50,
    status: str | None = None,
    before_created_at: str | None = None,
    before_job_id: str | None = None,
) -> list:
    """Paginated job list for an agent (worker view); keyset-paginated on ``before_created_at``/``before_job_id``."""
    limit = min(max(1, limit), 200)
    where_clauses = ["agent_id = %s"]
    params: list = [agent_id]
    if status:
        where_clauses.append("status = %s")
        params.append(status)
    if before_created_at:
        cursor_job_id = before_job_id or "\uffff"
        where_clauses.append("(created_at < %s OR (created_at = %s AND job_id < %s))")
        params.extend([before_created_at, before_created_at, cursor_job_id])
    where_sql = " AND ".join(where_clauses)
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {where_sql}
            ORDER BY created_at DESC, job_id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_jobs_for_agent_in_states(
    agent_id: str,
    *,
    states: tuple[str, ...],
    limit: int = 200,
) -> list:
    """Return jobs for an agent currently in any of the given statuses.

    Used by admin agent-delete to enumerate in-flight jobs that must be
    cancelled and refunded before the agent row can be removed.
    """
    if not states:
        return []
    capped = min(max(1, int(limit)), 1000)
    placeholders = ", ".join(["%s"] * len(states))
    params: list = [agent_id, *states, capped]
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE agent_id = %s AND status IN ({placeholders})
            ORDER BY created_at DESC, job_id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
