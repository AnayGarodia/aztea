# OWNS: wallet_holds lifecycle (create/consume/release), held_cents cache moves,
#       sweeper for expired holds, hold-amount calculation from payout curves.
# NOT OWNS: ledger inserts (base.py:_insert_tx), payout-curve fraction math
#       (payout_curve.fraction_for_rating), withdrawal gating (route layer).
# INVARIANTS:
#   - wallets.held_cents == SUM(amount_cents FROM wallet_holds WHERE wallet_id=? AND status='active').
#     Every status flip MUST be in the same DB transaction as the matching
#     UPDATE wallets SET held_cents = held_cents +/- amount.
#   - wallet_holds_job_uq enforces one hold per job_id; settlement replays
#     are no-ops, NOT duplicate holds.
#   - Hold amount must NEVER exceed the agent's payout for the job. Sized
#     from compute_hold_cents() at settlement time.
# DECISIONS:
#   - Hold window = job.dispute_window_hours (per-job, default 72). Late
#     ratings beyond the window fall through to the defense-in-depth path
#     in payout_curve.apply_curve_clawback. There is no separate global
#     RATING_WINDOW_HOURS in this codebase.
# KNOWN DEBT:
#   - No partial-hold consumption with carryover yet: a partial clawback
#     consumes the full hold (claws the requested cents, releases the rest
#     immediately). That matches the user-spec but rules out a future
#     "partial dispute now, second dispute later on the same job" flow.

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from core import db as _db

from .base import _conn

_LOG = logging.getLogger(__name__)

# Sweeper batch cap — keeps any single tick from holding the write lock
# longer than necessary. The hold window is hours, not seconds, so processing
# 100 expired holds per minute is more than enough.
_RELEASE_SWEEP_BATCH_LIMIT = 100

# Reasons recorded on release_reason; kept as named constants so tests and
# the dashboard query the same strings.
RELEASE_REASON_WINDOW_EXPIRED = "window_expired"
RELEASE_REASON_RATING_RELEASE = "rating_release"
RELEASE_REASON_RATING_CLAWBACK = "rating_clawback"
RELEASE_REASON_DISPUTE_CLAWBACK = "dispute_clawback"

_VALID_CONSUME_REASONS = (
    RELEASE_REASON_RATING_CLAWBACK,
    RELEASE_REASON_DISPUTE_CLAWBACK,
)

_VALID_RELEASE_REASONS = (
    RELEASE_REASON_WINDOW_EXPIRED,
    RELEASE_REASON_RATING_RELEASE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hold_until_iso(dispute_window_hours: int) -> str:
    """Return an ISO timestamp dispute_window_hours from now.

    The caller validates the window (jobs.create_job already requires >= 1).
    A non-positive value here yields hold_until == now, which means the
    sweeper will release the hold on its very next tick — safe but useless.
    """
    hours = max(1, int(dispute_window_hours or 0))
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def compute_hold_cents(
    agent_payout_cents: int,
    payout_curve: dict[str, float] | None,
) -> int:
    """Worst-case clawback the curve can take from this payout.

    Hold amount = agent_payout_cents * (1 - min_fraction). With no curve
    defined the agent has zero floor, so the entire payout is at risk and
    the entire amount is held. With min_fraction == 1.0 (all ratings keep
    100% of payout) nothing is at risk and the hold is zero — the agent
    can withdraw immediately.

    Pure function. Caller does the DB work.
    """
    payout = max(0, int(agent_payout_cents or 0))
    if payout == 0:
        return 0
    if not payout_curve:
        return payout
    try:
        fractions = [float(v) for v in payout_curve.values()]
    except (TypeError, ValueError):
        return payout
    if not fractions:
        return payout
    min_fraction = max(0.0, min(min(fractions), 1.0))
    at_risk_fraction = 1.0 - min_fraction
    if at_risk_fraction <= 0:
        return 0
    # Round UP — over-reserving by 1 cent is safer than under-reserving and
    # leaving an unbacked clawback. The released remainder cleans it up.
    held = -(-payout * int(round(at_risk_fraction * 1_000_000)) // 1_000_000)
    return min(payout, max(0, int(held)))


def get_active_hold_for_job_conn(
    conn: _db.DbConnection, job_id: str
) -> dict | None:
    """Return the active hold for a job, if any. Read-only."""
    row = conn.execute(
        """
        SELECT hold_id, wallet_id, job_id, amount_cents, created_at,
               hold_until, status, released_at, clawback_cents, release_reason
        FROM wallet_holds
        WHERE job_id = %s AND status = 'active'
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def create_hold_conn(
    conn: _db.DbConnection,
    *,
    wallet_id: str,
    job_id: str,
    amount_cents: int,
    dispute_window_hours: int,
) -> dict | None:
    """Insert a hold + bump wallet.held_cents inside the caller's transaction.

    The caller is responsible for opening the transaction and committing.
    Returns the inserted hold row, or None if amount_cents <= 0 (no hold to
    create) or the hold already exists for this job (idempotent settlement
    replay).
    """
    if int(amount_cents) <= 0:
        return None

    existing = get_active_hold_for_job_conn(conn, job_id)
    if existing is not None:
        return existing

    try:
        hold_id = str(uuid.uuid4())
        created_at = _now_iso()
        hold_until = _hold_until_iso(dispute_window_hours)
        conn.execute(
            """
            INSERT INTO wallet_holds (
                hold_id, wallet_id, job_id, amount_cents, created_at,
                hold_until, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'active')
            """,
            (hold_id, wallet_id, job_id, int(amount_cents), created_at, hold_until),
        )
    except _db.IntegrityError:
        # wallet_holds_job_uq fired — concurrent settlement won the race.
        # Re-fetch and return the existing row so the caller still sees a
        # consistent snapshot.
        return get_active_hold_for_job_conn(conn, job_id)

    cur = conn.execute(
        """
        UPDATE wallets
        SET held_cents = held_cents + %s
        WHERE wallet_id = %s
        """,
        (int(amount_cents), wallet_id),
    )
    if getattr(cur, "rowcount", 1) == 0:
        # Race guard mirrors the pre_call_charge / post_call_payout pattern:
        # if the wallet vanished mid-transaction, refuse to leave the hold
        # row stranded.
        raise LookupError(f"Wallet '{wallet_id}' not found while creating hold.")

    return get_active_hold_for_job_conn(conn, job_id)


def consume_hold_conn(
    conn: _db.DbConnection,
    *,
    job_id: str,
    clawback_cents: int,
    reason: str,
) -> dict | None:
    """Mark the active hold as consumed and decrement wallet.held_cents.

    Behaviour:
      * clawback_cents == 0       -> caller should use release_hold_conn instead;
                                     this function refuses to mark a hold consumed
                                     for zero cents and returns None.
      * 0 < clawback_cents < hold -> status='clawed_partial', clawback_cents recorded.
                                     The remainder of the hold releases immediately
                                     (held_cents drops by hold.amount, not clawback).
      * clawback_cents >= hold    -> status='clawed_full', clawback capped at hold.

    Returns the hold row after the update, or None if no active hold exists.
    The caller is still responsible for the actual money movement (debit
    agent / credit caller-or-escrow) — this function only adjusts the hold
    accounting.
    """
    if reason not in _VALID_CONSUME_REASONS:
        raise ValueError(
            f"consume_hold_conn: reason {reason!r} not in {_VALID_CONSUME_REASONS}"
        )
    if int(clawback_cents) <= 0:
        return None

    hold = get_active_hold_for_job_conn(conn, job_id)
    if hold is None:
        return None

    hold_amount = int(hold["amount_cents"])
    consumed = min(int(clawback_cents), hold_amount)
    new_status = "clawed_full" if consumed >= hold_amount else "clawed_partial"

    cur = conn.execute(
        """
        UPDATE wallet_holds
        SET status = %s, released_at = %s, clawback_cents = %s, release_reason = %s
        WHERE hold_id = %s AND status = 'active'
        """,
        (new_status, _now_iso(), consumed, reason, hold["hold_id"]),
    )
    if getattr(cur, "rowcount", 1) == 0:
        # Concurrent path consumed it first.
        return get_active_hold_for_job_conn(conn, job_id)

    cur = conn.execute(
        """
        UPDATE wallets
        SET held_cents = held_cents - %s
        WHERE wallet_id = %s AND held_cents >= %s
        """,
        (hold_amount, hold["wallet_id"], hold_amount),
    )
    if getattr(cur, "rowcount", 1) == 0:
        raise LookupError(
            f"Wallet '{hold['wallet_id']}' had insufficient held_cents "
            f"({hold_amount}¢) when consuming hold {hold['hold_id']}."
        )

    refreshed = conn.execute(
        """
        SELECT hold_id, wallet_id, job_id, amount_cents, created_at,
               hold_until, status, released_at, clawback_cents, release_reason
        FROM wallet_holds WHERE hold_id = %s
        """,
        (hold["hold_id"],),
    ).fetchone()
    return dict(refreshed) if refreshed is not None else None


def release_hold_conn(
    conn: _db.DbConnection,
    *,
    job_id: str,
    reason: str,
) -> dict | None:
    """Release an active hold without taking any clawback.

    Used when:
      * a 5-star (or curve-floor) rating arrives before the window closes,
      * the sweeper trips the hold_until timer with no rating/dispute filed.

    Returns the hold row after the update, or None if no active hold exists.
    """
    if reason not in _VALID_RELEASE_REASONS:
        raise ValueError(
            f"release_hold_conn: reason {reason!r} not in {_VALID_RELEASE_REASONS}"
        )

    hold = get_active_hold_for_job_conn(conn, job_id)
    if hold is None:
        return None

    hold_amount = int(hold["amount_cents"])
    cur = conn.execute(
        """
        UPDATE wallet_holds
        SET status = 'released', released_at = %s, release_reason = %s
        WHERE hold_id = %s AND status = 'active'
        """,
        (_now_iso(), reason, hold["hold_id"]),
    )
    if getattr(cur, "rowcount", 1) == 0:
        return get_active_hold_for_job_conn(conn, job_id)

    cur = conn.execute(
        """
        UPDATE wallets
        SET held_cents = held_cents - %s
        WHERE wallet_id = %s AND held_cents >= %s
        """,
        (hold_amount, hold["wallet_id"], hold_amount),
    )
    if getattr(cur, "rowcount", 1) == 0:
        raise LookupError(
            f"Wallet '{hold['wallet_id']}' had insufficient held_cents "
            f"({hold_amount}¢) when releasing hold {hold['hold_id']}."
        )

    return {**hold, "status": "released", "release_reason": reason}


def release_expired_holds(*, limit: int = _RELEASE_SWEEP_BATCH_LIMIT) -> int:
    """Sweeper entry point: release every hold whose window has closed.

    Returns the count released this tick. Each hold release runs in its own
    transaction so a single bad row doesn't block the rest of the batch.
    """
    released = 0
    cutoff = _now_iso()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT job_id
            FROM wallet_holds
            WHERE status = 'active' AND hold_until < %s
            ORDER BY hold_until ASC
            LIMIT %s
            """,
            (cutoff, int(limit)),
        ).fetchall()
    for row in rows:
        job_id = str(row["job_id"])
        try:
            with _conn() as inner:
                inner.execute("BEGIN IMMEDIATE")
                result = release_hold_conn(
                    inner, job_id=job_id, reason=RELEASE_REASON_WINDOW_EXPIRED
                )
            if result is not None:
                released += 1
        except Exception as exc:  # pragma: no cover — defensive
            _LOG.warning(
                "wallet_holds.release_expired_holds: job_id=%s failed: %s",
                job_id,
                exc,
            )
    if released:
        _LOG.info("wallet_holds.release_expired_holds released=%d", released)
    return released


def sum_active_held_cents_for_wallet(wallet_id: str) -> int:
    """Reconciliation helper: SUM(amount_cents) of active holds for a wallet."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM wallet_holds
            WHERE wallet_id = %s AND status = 'active'
            """,
            (wallet_id,),
        ).fetchone()
    return int(row["total"] or 0) if row is not None else 0


def init_wallet_holds_db(conn: _db.DbConnection) -> None:
    """Idempotent SQLite-only schema init mirroring 0046_wallet_holds.sql.

    Postgres deployments rely solely on the migration. SQLite test runs and
    fresh dev DBs that bypass the migration runner pick up the schema here
    via the same path init_payments_db() takes for wallets/transactions.
    """
    if _db.IS_POSTGRES:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_holds (
            hold_id          TEXT PRIMARY KEY,
            wallet_id        TEXT NOT NULL REFERENCES wallets(wallet_id),
            job_id           TEXT NOT NULL,
            amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
            created_at       TEXT NOT NULL,
            hold_until       TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'released', 'clawed_full', 'clawed_partial')),
            released_at      TEXT NULL,
            clawback_cents   INTEGER NULL,
            release_reason   TEXT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS wallet_holds_job_uq ON wallet_holds(job_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS wallet_holds_active_wallet_idx "
        "ON wallet_holds(wallet_id) WHERE status = 'active'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS wallet_holds_active_hold_until_idx "
        "ON wallet_holds(hold_until) WHERE status = 'active'"
    )
    # Add wallets.held_cents idempotently via the same _add_column_if_missing
    # pattern init_payments_db() uses. We can't import that helper without a
    # cycle, so call it inline here (same shape, same safety).
    try:
        conn.execute(
            "ALTER TABLE wallets ADD COLUMN held_cents INTEGER NOT NULL DEFAULT 0"
        )
    except _db.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise
