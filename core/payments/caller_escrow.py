"""Caller-side escrow for async jobs.

# OWNS: reserve / consume / release of caller funds for an async job
#       lifecycle. Distinct from wallet_holds (agent-side payout hold).
# NOT OWNS: the ledger itself — every real money move still happens via
#           core/payments/base.py. The escrow table only records the
#           *intent* to debit; the actual debit fires inside
#           consume_caller_escrow.
# INVARIANTS:
#   * Exactly one escrow row per job_id (PRIMARY KEY).
#   * An escrow row's resolution is terminal — once consumed or
#     released, it stays that way (and the wallet_holds /
#     transactions ledger is the audit trail).
#   * If the feature flag is off, none of these helpers run from the
#     hot path. The migration is still applied so a toggle-on requires
#     no schema change.
# DECISIONS:
#   * Kept tiny on purpose: this isn't a second ledger, just a holding
#     pen between the create and complete states.
# KNOWN DEBT: balance_cents accounting for "available vs held" on the
#             caller side is not yet reflected in API responses
#             (frontend still shows the full balance). When this gates
#             on, the wallet detail view needs an ``available_cents``
#             field added.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from core import db as _db

_LOG = logging.getLogger(__name__)

_DEFAULT_ESCROW_HOURS = 48
ESCROW_STATUS_ACTIVE = "active"
ESCROW_STATUS_CONSUMED = "consumed"
ESCROW_STATUS_RELEASED = "released"

# Sentinel charge_tx_id used while a job is escrowed but not yet charged.
# Format: ``ESCROW_TX_PREFIX + job_id``. post_call_payout / post_call_refund
# recognise this prefix and translate it back to a real ledger movement
# at settlement time (lazy debit) instead of treating it as an existing
# charge tx that needs unwinding.
ESCROW_TX_PREFIX = "escrow:"


def is_escrow_tx_id(tx_id: object) -> bool:
    """Pure: True when ``tx_id`` is an escrow sentinel placeholder."""
    return isinstance(tx_id, str) and tx_id.startswith(ESCROW_TX_PREFIX)


def job_id_from_sentinel(tx_id: str) -> str:
    """Pure: inverse of ``ESCROW_TX_PREFIX + job_id``."""
    if not is_escrow_tx_id(tx_id):
        raise ValueError(f"{tx_id!r} is not an escrow sentinel.")
    return tx_id[len(ESCROW_TX_PREFIX):]


def caller_escrow_enabled() -> bool:
    """Pure: env flag for the deep-tier escrow change. Default OFF.

    Defaults off so existing async-job money flow (immediate charge +
    refund-on-fail) keeps working. Turn on with
    ``AZTEA_CALLER_ESCROW_ENABLED=1``.
    """
    return os.environ.get("AZTEA_CALLER_ESCROW_ENABLED", "0").lower() in {
        "1", "true", "yes", "on",
    }


def _escrow_hours() -> int:
    try:
        return max(1, int(os.environ.get(
            "AZTEA_CALLER_ESCROW_HOURS", str(_DEFAULT_ESCROW_HOURS),
        )))
    except (TypeError, ValueError):
        return _DEFAULT_ESCROW_HOURS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reserve(
    conn: _db.DbConnection,
    *,
    job_id: str,
    caller_wallet_id: str,
    amount_cents: int,
    expires_at_iso: str | None = None,
) -> dict[str, Any]:
    """Side-effect: reserve ``amount_cents`` against ``caller_wallet_id`` for ``job_id``.

    Does NOT debit the wallet. The reservation is consumed (debited) on
    job complete or released on failure. Idempotent on job_id — a second
    reserve call for the same job is a no-op and returns the existing row.

    Raises ``ValueError`` if amount_cents <= 0.
    """
    amount = int(amount_cents)
    if amount <= 0:
        raise ValueError("amount_cents must be > 0")
    existing = conn.execute(
        "SELECT job_id, caller_wallet_id, amount_cents, status FROM job_caller_escrow "
        "WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    if existing is not None:
        # Idempotency: same job_id was already reserved.
        return {k: existing[k] for k in existing.keys()}
    now_iso = _now().isoformat()
    expires_iso = expires_at_iso or (
        _now() + timedelta(hours=_escrow_hours())
    ).isoformat()
    conn.execute(
        "INSERT INTO job_caller_escrow "
        "(job_id, caller_wallet_id, amount_cents, created_at, expires_at, status) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            job_id, caller_wallet_id, amount, now_iso, expires_iso,
            ESCROW_STATUS_ACTIVE,
        ),
    )
    # Hold the funds on the caller's wallet so withdrawal sees them as
    # unavailable (the withdrawal gate already enforces
    # balance_cents - held_cents >= request). The same held_cents column
    # is shared with wallet_holds (agent-side) — that's intentional and
    # safe: the column tracks total reservations across all purposes.
    conn.execute(
        "UPDATE wallets SET held_cents = held_cents + %s WHERE wallet_id = %s",
        (amount, caller_wallet_id),
    )
    return {
        "job_id": job_id,
        "caller_wallet_id": caller_wallet_id,
        "amount_cents": amount,
        "created_at": now_iso,
        "expires_at": expires_iso,
        "status": ESCROW_STATUS_ACTIVE,
    }


def consume(
    conn: _db.DbConnection,
    *,
    job_id: str,
    note: str | None = None,
) -> dict[str, Any] | None:
    """Side-effect: mark the escrow as consumed (the debit has been booked).

    Idempotent: calling on an already-consumed row is a no-op. Returns
    the row state, or None if the job had no escrow.
    """
    row = conn.execute(
        "SELECT * FROM job_caller_escrow WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    current_status = str(row["status"] or "").strip()
    if current_status == ESCROW_STATUS_CONSUMED:
        return {k: row[k] for k in row.keys()}
    if current_status != ESCROW_STATUS_ACTIVE:
        # Already released; refuse to consume — caller bug.
        raise ValueError(
            f"job_caller_escrow for {job_id} is in status {current_status!r}; "
            "cannot consume."
        )
    amount = int(row["amount_cents"] or 0)
    wallet_id = str(row["caller_wallet_id"] or "")
    conn.execute(
        "UPDATE job_caller_escrow "
        "SET status = %s, resolved_at = %s, resolution_note = %s "
        "WHERE job_id = %s AND status = %s",
        (
            ESCROW_STATUS_CONSUMED, _now().isoformat(), note or None,
            job_id, ESCROW_STATUS_ACTIVE,
        ),
    )
    # Release the held_cents reservation; the caller of consume() is
    # responsible for actually moving the money (debit caller + payout
    # agent) inside the same transaction. The reservation existed only
    # to gate withdrawals during the job's lifecycle.
    if amount > 0 and wallet_id:
        # CASE keeps the arithmetic SQLite + Postgres portable. MAX(0, ...)
        # is scalar on SQLite but aggregate-only on Postgres.
        conn.execute(
            "UPDATE wallets "
            "SET held_cents = CASE WHEN held_cents > %s THEN held_cents - %s "
            "ELSE 0 END "
            "WHERE wallet_id = %s",
            (amount, amount, wallet_id),
        )
    row = conn.execute(
        "SELECT * FROM job_caller_escrow WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    return {k: row[k] for k in row.keys()} if row else None


def release(
    conn: _db.DbConnection,
    *,
    job_id: str,
    note: str | None = None,
) -> dict[str, Any] | None:
    """Side-effect: mark the escrow as released (caller keeps their funds).

    Idempotent. Returns the row state, or None if no escrow existed.
    """
    row = conn.execute(
        "SELECT * FROM job_caller_escrow WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    current_status = str(row["status"] or "").strip()
    if current_status == ESCROW_STATUS_RELEASED:
        return {k: row[k] for k in row.keys()}
    if current_status != ESCROW_STATUS_ACTIVE:
        # Already consumed; refuse to release.
        raise ValueError(
            f"job_caller_escrow for {job_id} is in status {current_status!r}; "
            "cannot release."
        )
    amount = int(row["amount_cents"] or 0)
    wallet_id = str(row["caller_wallet_id"] or "")
    conn.execute(
        "UPDATE job_caller_escrow "
        "SET status = %s, resolved_at = %s, resolution_note = %s "
        "WHERE job_id = %s AND status = %s",
        (
            ESCROW_STATUS_RELEASED, _now().isoformat(), note or None,
            job_id, ESCROW_STATUS_ACTIVE,
        ),
    )
    if amount > 0 and wallet_id:
        # CASE keeps the arithmetic SQLite + Postgres portable. MAX(0, ...)
        # is scalar on SQLite but aggregate-only on Postgres.
        conn.execute(
            "UPDATE wallets "
            "SET held_cents = CASE WHEN held_cents > %s THEN held_cents - %s "
            "ELSE 0 END "
            "WHERE wallet_id = %s",
            (amount, amount, wallet_id),
        )
    row = conn.execute(
        "SELECT * FROM job_caller_escrow WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    return {k: row[k] for k in row.keys()} if row else None


def active_for_wallet(
    conn: _db.DbConnection, wallet_id: str,
) -> int:
    """Pure-ish: SUM of active escrow amounts for a wallet.

    Used to compute caller's "available" balance for the rare callers
    that want it: balance_cents - active_caller_escrow = available.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS total "
        "FROM job_caller_escrow WHERE caller_wallet_id = %s AND status = %s",
        (wallet_id, ESCROW_STATUS_ACTIVE),
    ).fetchone()
    return int(row["total"] or 0) if row else 0


def reserve_for_async_job(
    *,
    job_id: str,
    caller_wallet_id: str,
    amount_cents: int,
    require_balance: bool = True,
) -> str:
    """Side-effect: validate balance + create escrow + return a sentinel tx_id.

    Returns ``ESCROW_TX_PREFIX + job_id``. This sentinel is stored on
    ``jobs.charge_tx_id`` in lieu of a real ledger entry; the actual
    debit fires later via ``post_call_payout`` (success) or never fires
    (release on failure). Raises ``ValueError`` on a balance check
    failure so the caller can surface a structured 402.
    """
    amount = int(amount_cents)
    if amount < 0:
        raise ValueError("amount_cents must be ≥ 0")
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        if require_balance and amount > 0:
            row = conn.execute(
                "SELECT balance_cents, held_cents FROM wallets WHERE wallet_id = %s",
                (caller_wallet_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Wallet '{caller_wallet_id}' not found.")
            available = int(row["balance_cents"] or 0) - int(row["held_cents"] or 0)
            if available < amount:
                raise ValueError(
                    f"insufficient_available_balance: have {available}¢, need {amount}¢"
                )
        if amount > 0:
            reserve(
                conn,
                job_id=job_id,
                caller_wallet_id=caller_wallet_id,
                amount_cents=amount,
            )
        conn.commit()
    return ESCROW_TX_PREFIX + job_id


def settle_escrow_to_charge(
    conn: _db.DbConnection,
    *,
    job_id: str,
    agent_id: str,
    note: str = "escrow_consumed",
) -> str | None:
    """Side-effect: convert an active escrow into a real debit + charge tx.

    Used by ``post_call_payout`` when it receives a sentinel tx_id. The
    wallet debit happens here (not at job-creation time), creating a
    fresh ``charge_tx_id`` that the rest of the payout pipeline uses.

    Returns the new charge_tx_id, or None when no active escrow exists
    (already consumed / released — caller treats the job as a no-op).
    """
    # Late-import to avoid circular dependency: payments.base imports
    # caller_escrow for sentinel detection.
    from core.payments import base as _payments_base
    row = conn.execute(
        "SELECT * FROM job_caller_escrow WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    if row is None:
        _LOG.warning("settle_escrow_to_charge: no escrow row for job %s", job_id)
        return None
    status = str(row["status"] or "").strip()
    if status != ESCROW_STATUS_ACTIVE:
        # Already settled by an earlier call — idempotent return.
        return None
    amount = int(row["amount_cents"] or 0)
    wallet_id = str(row["caller_wallet_id"] or "")
    # Decrement held_cents first (atomic with the debit) so the
    # withdrawal gate doesn't see the same funds locked twice.
    consume(conn, job_id=job_id, note=note)
    if amount <= 0:
        # Free-tier call — nothing to debit but the escrow still resolves.
        return None
    # Insert the real charge row. Reuse _insert_tx so the ledger
    # invariant ("transactions are insert-only") stays clean.
    tx_id = _payments_base._insert_tx(
        conn, wallet_id, "charge", amount, None, agent_id,
        f"escrow_consumed:{job_id}",
    )
    conn.execute(
        "UPDATE wallets SET balance_cents = balance_cents - %s WHERE wallet_id = %s",
        (amount, wallet_id),
    )
    return tx_id


def release_expired(*, limit: int = 100) -> int:
    """Side-effect: release escrow rows whose expiry has passed.

    Called by the daily sweeper. Returns the count of rows released.
    """
    capped = min(max(1, int(limit)), 500)
    now_iso = _now().isoformat()
    released = 0
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT job_id FROM job_caller_escrow "
            "WHERE status = %s AND expires_at < %s LIMIT %s",
            (ESCROW_STATUS_ACTIVE, now_iso, capped),
        ).fetchall()
        for r in rows or []:
            try:
                result = release(
                    conn, job_id=str(r["job_id"]),
                    note="window_expired",
                )
                if result is not None:
                    released += 1
            except ValueError:
                continue
        conn.commit()
    return released
