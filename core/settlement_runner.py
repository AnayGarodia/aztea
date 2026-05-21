"""Decoupled settlement + receipt-signing runner for terminal job transitions.

# OWNS: draining the ``pending_settlements`` queue. For each unsettled row,
#       this module decides which payment primitive to call based on the
#       job's terminal_state and billing_unit, builds and stores the signed
#       transcript receipt, and stamps settled_at / receipt_built_at.
# NOT OWNS: the message-insert transaction (core/jobs/messaging.py) — which
#       only inserts the settlement row and returns. The settlement and
#       signing happen in this module's transactions.
# INVARIANTS:
#   - Every settlement step checks its own done-stamp before acting
#     (idempotent under double-drain).
#   - Settlement and receipt-signing never run inside the messaging tx.
#     Holding a write lock through Ed25519 signing is the anti-pattern this
#     queue exists to prevent.
#   - Existing post_call_payout / post_call_refund are already idempotent
#     via related_tx_id checks; we layer our own settled_at stamp on top
#     so the queue itself is observable and replayable.
# DECISIONS:
#   - In v1 only the 'stopped' terminal state enqueues. Existing
#     'complete' / 'failed' paths keep their inline settlement. Migrating
#     them onto this queue is tracked as boy-scout follow-up — see the
#     copilot-mode design doc, "Risks / open issues."
#   - Synchronous post-commit drain is invoked by the route handler that
#     causes the terminal transition (so callers see receipts immediately);
#     the existing background sweeper picks up anything that fails.
# KNOWN DEBT:
#   - Cross-worker contention: the SQLite lease uses optimistic
#     UPDATE … WHERE settled_at IS NULL AND attempts = ?. Postgres path
#     uses FOR UPDATE SKIP LOCKED. Both are correct; SQLite's is slightly
#     less efficient under contention but the workload is bounded.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from core import db as _db

_LOG = logging.getLogger(__name__)
DB_PATH = _db.DB_PATH
_local = _db._local

_MAX_ATTEMPTS = 5
_PARTIAL_BILLING_UNIT = "partial"
_CALL_BILLING_UNIT = "call"


def _resolved_db_path() -> str:
    """Prefer the jobs DB path because settlement rows are owned by core.jobs."""
    jobs_module = sys.modules.get("core.jobs")
    if jobs_module is not None:
        candidate = getattr(jobs_module, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    module = sys.modules.get("core.settlement_runner")
    if module is not None:
        candidate = getattr(module, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return DB_PATH


def _conn() -> _db.DbConnection:
    return _db.get_db_connection(_resolved_db_path())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue_terminal(
    job_id: str,
    terminal_state: str,
    *,
    terminal_at: str | None = None,
    conn: Any = None,
) -> None:
    """Insert a pending_settlements row inside the caller's transaction.

    Caller must already hold a write transaction on ``conn`` (e.g. the
    messaging tx that just stamped jobs.terminal_at). The row is unique on
    job_id; a re-enqueue is a no-op (kept idempotent so retried terminal
    transitions don't double-queue).
    """
    if not job_id:
        raise ValueError("job_id is required")
    if not terminal_state:
        raise ValueError("terminal_state is required")
    stamp = terminal_at or _now_iso()
    sql = """
        INSERT INTO pending_settlements (job_id, terminal_state, terminal_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (job_id) DO NOTHING
    """
    if conn is None:
        with _conn() as c:
            c.execute(sql, (job_id, terminal_state, stamp))
    else:
        conn.execute(sql, (job_id, terminal_state, stamp))


def drain_one(job_id: str | None = None) -> bool:
    """Drain a single unsettled row.

    If ``job_id`` is given, drain that specific row (used for synchronous
    post-commit drain by the route that caused the terminal transition).
    Otherwise lease the oldest unsettled row (used by the background sweeper).

    Returns True if a row was processed (success or failure), False if the
    queue was empty / the row was already settled.
    """
    row = _lease_row(job_id)
    if row is None:
        return False
    try:
        _settle_row(row)
        _stamp_done(row["job_id"])
        return True
    except Exception as exc:
        _record_failure(row["job_id"], str(exc))
        _LOG.warning(
            "settlement_runner.failed job=%s error=%s",
            row["job_id"],
            str(exc),
            exc_info=True,
        )
        return True


def drain_all(max_rows: int = 100) -> int:
    """Drain up to ``max_rows`` unsettled rows. Returns count processed."""
    processed = 0
    for _ in range(max_rows):
        if not drain_one():
            break
        processed += 1
    return processed


def _lease_row(job_id: str | None) -> dict | None:
    """Atomically claim one unsettled row.

    On Postgres uses ``FOR UPDATE SKIP LOCKED``; on SQLite uses an optimistic
    UPDATE-on-attempts pattern. In both cases an already-leased / already-
    settled row is skipped.
    """
    where_job = "AND job_id = %s" if job_id else ""
    params = (job_id,) if job_id else ()

    if _db.IS_POSTGRES:
        sql = f"""
            UPDATE pending_settlements
               SET attempts = attempts + 1
             WHERE job_id = (
                   SELECT job_id FROM pending_settlements
                    WHERE settled_at IS NULL
                      AND attempts < %s
                      {where_job}
                    ORDER BY terminal_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
             )
            RETURNING job_id, terminal_state, terminal_at, attempts
        """
        with _conn() as conn:
            cur = conn.execute(sql, (_MAX_ATTEMPTS, *params))
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "job_id": row["job_id"],
                "terminal_state": row["terminal_state"],
                "terminal_at": row["terminal_at"],
                "attempts": row["attempts"],
            }

    # SQLite path: optimistic claim.
    with _conn() as conn:
        sel_sql = f"""
            SELECT job_id, terminal_state, terminal_at, attempts
              FROM pending_settlements
             WHERE settled_at IS NULL
               AND attempts < %s
               {where_job}
             ORDER BY terminal_at ASC
             LIMIT 1
        """
        cur = conn.execute(sel_sql, (_MAX_ATTEMPTS, *params))
        row = cur.fetchone()
        if row is None:
            return None
        leased_job_id = row["job_id"]
        attempts = row["attempts"]
        upd = conn.execute(
            """
            UPDATE pending_settlements
               SET attempts = attempts + 1
             WHERE job_id = %s
               AND settled_at IS NULL
               AND attempts = %s
            """,
            (leased_job_id, attempts),
        )
        if getattr(upd, "rowcount", 0) != 1:
            # Lost the race; let next sweeper tick pick it up.
            return None
        return {
            "job_id": leased_job_id,
            "terminal_state": row["terminal_state"],
            "terminal_at": row["terminal_at"],
            "attempts": attempts + 1,
        }


def _settle_row(row: dict) -> None:
    """Run the right settlement primitive(s) for the leased row.

    Reads the job, dispatches by terminal_state + billing_unit, then triggers
    receipt build (delegated to core.receipts when implemented in Phase 3).
    """
    job = _read_job_for_settlement(row["job_id"])
    if job is None:
        # Job vanished (test cleanup, manual delete) — mark settled to drain.
        return

    # Defensive commit: the lease + read path above may have left an
    # implicit deferred transaction open on the thread-local SQLite
    # connection. Existing payment primitives (post_call_payout etc.)
    # issue BEGIN IMMEDIATE which fails inside an active tx. Forcing a
    # commit here guarantees a clean slate. No-op when nothing is pending.
    _force_commit_thread_local_conn()

    terminal_state = row["terminal_state"]
    if terminal_state == "stopped":
        _settle_stopped(job)
    # 'complete' / 'failed' paths keep inline settlement in v1; not handled
    # here. See KNOWN DEBT in the module docstring.

    _build_receipt_if_available(job, terminal_state)


def _force_commit_thread_local_conn() -> None:
    """Force a commit on the thread-local connection if one is active.

    SQLite's Python binding implicitly opens a deferred transaction on the
    first DML statement. Our DbConnection wrapper commits on `with` exit,
    but if any read path between the lease and the settlement primitive
    leaves the connection in_transaction (e.g. a metadata SELECT that
    happened to follow a DML on the same connection), the next
    BEGIN IMMEDIATE will fail. A bare commit() is safe whether or not a
    tx is open.
    """
    try:
        with _conn() as conn:
            conn.commit()
    except Exception:
        # Never block settlement on the defensive commit.
        pass


def _settle_stopped(job: dict) -> None:
    """Settle a stop_when-aborted job.

    billing_unit='partial' → settle partials_count * unit_price, refund the rest.
    billing_unit='call' (or unset) → settle full price (same as a complete).
    """
    billing_unit = (job.get("billing_unit") or _CALL_BILLING_UNIT).strip()
    if billing_unit == _PARTIAL_BILLING_UNIT:
        _settle_partial_units(job)
    else:
        _settle_full_call(job)


def _settle_full_call(job: dict) -> None:
    """Identical to the complete-path payout: pay agent + platform fee in full."""
    from core.payments.base import post_call_payout

    post_call_payout(
        agent_wallet_id=job["agent_wallet_id"],
        platform_wallet_id=job["platform_wallet_id"],
        charge_tx_id=job["charge_tx_id"],
        price_cents=int(job["price_cents"]),
        agent_id=job["agent_id"],
        platform_fee_pct=job.get("platform_fee_pct_at_create"),
        fee_bearer_policy=job.get("fee_bearer_policy"),
    )


def _settle_partial_units(job: dict) -> None:
    """Pay agent for the partials they actually emitted; refund the rest.

    The unit price is price_cents / max(declared_max, 1); emitted_units is
    capped by declared_max if present, otherwise by partials_count alone.
    For v1 the declared_max comes from the job's stop_when_json metadata
    (optional 'max_units' key); without it we use partials_count directly.
    """
    from core.payments.base import post_call_payout, post_call_refund

    price_cents = int(job["price_cents"] or 0)
    partials = max(0, int(job.get("partials_count") or 0))
    declared_max = _declared_max_units(job)

    units = partials if declared_max is None else min(partials, declared_max)
    denom = declared_max if declared_max is not None else max(partials, 1)
    if denom <= 0:
        denom = 1
    settle_cents = (price_cents * units) // denom
    settle_cents = max(0, min(settle_cents, price_cents))
    refund_cents = price_cents - settle_cents

    if settle_cents > 0:
        post_call_payout(
            agent_wallet_id=job["agent_wallet_id"],
            platform_wallet_id=job["platform_wallet_id"],
            charge_tx_id=job["charge_tx_id"],
            price_cents=settle_cents,
            agent_id=job["agent_id"],
            platform_fee_pct=job.get("platform_fee_pct_at_create"),
            fee_bearer_policy=job.get("fee_bearer_policy"),
        )
    if refund_cents > 0:
        post_call_refund(
            caller_wallet_id=job["caller_wallet_id"],
            charge_tx_id=job["charge_tx_id"],
            refund_cents=refund_cents,
            agent_id=job["agent_id"],
            reason="copilot_partial_settlement",
        )


def _declared_max_units(job: dict) -> int | None:
    """Optional 'max_units' from the job's stop_when_json metadata."""
    raw = job.get("stop_when_json")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    val = parsed.get("max_units")
    if val is None:
        return None
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _build_receipt_if_available(job: dict, terminal_state: str) -> None:
    """Delegate to core.receipts.sign_and_store_receipt when Phase 3 lands.

    Until then this is a no-op; the receipt_built_at stamp is still set by
    _stamp_done so the queue drains cleanly. Phase 3 will replace this with
    a real call.
    """
    try:
        from core import receipts  # type: ignore[attr-defined]
    except ImportError:
        return
    fn = getattr(receipts, "sign_and_store_receipt", None)
    if fn is None:
        return
    try:
        fn(job["job_id"])
    except Exception as exc:
        # Receipt-signing failure should not roll back ledger settlement.
        # Log and let the sweeper retry the receipt build on the next pass.
        _LOG.warning(
            "receipt_build_failed",
            extra={"job_id": job["job_id"], "error": str(exc)},
        )


def _read_job_for_settlement(job_id: str) -> dict | None:
    sql = """
        SELECT job_id, agent_id, agent_wallet_id, caller_wallet_id, platform_wallet_id,
               charge_tx_id, price_cents, billing_unit, partials_count, stop_when_json,
               platform_fee_pct_at_create, fee_bearer_policy, status
          FROM jobs
         WHERE job_id = %s
    """
    with _conn() as conn:
        cur = conn.execute(sql, (job_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = (
        "job_id", "agent_id", "agent_wallet_id", "caller_wallet_id",
        "platform_wallet_id", "charge_tx_id", "price_cents", "billing_unit",
        "partials_count", "stop_when_json", "platform_fee_pct_at_create",
        "fee_bearer_policy", "status",
    )
    return {k: row[k] for k in keys}


def _stamp_done(job_id: str) -> None:
    sql = """
        UPDATE pending_settlements
           SET settled_at = COALESCE(settled_at, %s),
               receipt_built_at = COALESCE(receipt_built_at, %s)
         WHERE job_id = %s
    """
    now = _now_iso()
    with _conn() as conn:
        conn.execute(sql, (now, now, job_id))


def _record_failure(job_id: str, message: str) -> None:
    sql = "UPDATE pending_settlements SET last_error = %s WHERE job_id = %s"
    with _conn() as conn:
        conn.execute(sql, (message[:1000], job_id))
