"""Payments helpers for disputes and reconciliation.

Pairs with ``core.payments.base`` (which owns the wallet / ledger schema and
settlement primitives). This module implements the money movements that are
specific to the dispute lifecycle:

- **Filing deposits.** Dispute filers post a small escrow (``5% of job value``
  with a ``$0.05`` minimum) that is returned on win / split / void and
  forfeited to the platform on loss.
- **Escrow clawback.** When a caller wins a dispute on a job that already
  settled, this module compensates with a clawback entry against the agent
  wallet so the caller can be made whole without violating the insert-only
  invariant.
- **Settlement redistribution.** Dispute outcomes (``caller_wins``,
  ``agent_wins``, ``split``, ``void``) each produce a specific set of
  ledger entries that keep the balance invariant intact.
- **Reconciliation.** Background helpers compare ``wallets.balance_cents``
  against the ledger sum and surface mismatches via
  ``ops/payments/reconcile``.

All helpers require both the ledger + disputes state to be consistent, so
they run inside the shared ``_conn()`` transaction from ``core.payments.base``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

from core import db as _db
from core import logging_utils

from . import base as _payments_core

_conn = _payments_core._conn
_now = _payments_core._now
_insert_tx = _payments_core._insert_tx
_insert_tx_only = _payments_core._insert_tx_only
_resolve_charged_by_key_id = _payments_core._resolve_charged_by_key_id
get_or_create_wallet = _payments_core.get_or_create_wallet
get_wallet = _payments_core.get_wallet
get_wallet_by_owner = _payments_core.get_wallet_by_owner
set_wallet_daily_spend_limit = _payments_core.set_wallet_daily_spend_limit
charge = _payments_core.charge
get_wallet_transactions = _payments_core.get_wallet_transactions
get_agent_earnings_breakdown = _payments_core.get_agent_earnings_breakdown
list_connect_withdrawals = _payments_core.list_connect_withdrawals
deposit = _payments_core.deposit
pre_call_charge = _payments_core.pre_call_charge
post_call_payout = _payments_core.post_call_payout
post_call_refund = _payments_core.post_call_refund
post_call_partial_settle = _payments_core.post_call_partial_settle
normalize_fee_bearer_policy = _payments_core.normalize_fee_bearer_policy
compute_success_distribution = _payments_core.compute_success_distribution
compute_platform_fee_cents = _payments_core.compute_platform_fee_cents
PLATFORM_FEE_PCT = _payments_core.PLATFORM_FEE_PCT
DISPUTE_ESCROW_OWNER_PREFIX = _payments_core.DISPUTE_ESCROW_OWNER_PREFIX
DISPUTE_DEPOSIT_OWNER_PREFIX = _payments_core.DISPUTE_DEPOSIT_OWNER_PREFIX
DISPUTE_RETURN_PLATFORM_FEE_ON_CALLER_WINS = (
    _payments_core.DISPUTE_RETURN_PLATFORM_FEE_ON_CALLER_WINS
)
PLATFORM_OWNER_ID = _payments_core.PLATFORM_OWNER_ID
_LOG = _payments_core._LOG
InsufficientBalanceError = _payments_core.InsufficientBalanceError
KeySpendLimitExceededError = _payments_core.KeySpendLimitExceededError
WalletDailySpendLimitExceededError = _payments_core.WalletDailySpendLimitExceededError


def get_caller_trust(owner_id: str) -> float:
    wallet = get_or_create_wallet(owner_id)
    try:
        value = float(wallet.get("caller_trust", 0.5))
    except (TypeError, ValueError):
        value = 0.5
    return max(0.0, min(1.0, value))


def adjust_caller_trust(
    owner_id: str, *, delta: float, reason: str, related_id: str | None = None
) -> dict:
    """Insert a caller_trust adjustment row and update the running total.

    ``delta`` is a signed float (positive = improve trust, negative = reduce).
    Returns the updated trust record. Not idempotent — use ``adjust_caller_trust_once``
    when the same event should only be applied once.
    """
    normalized_owner_id = str(owner_id or "").strip()
    if not normalized_owner_id:
        raise ValueError("owner_id must be a non-empty string.")
    normalized_reason = str(reason or "").strip() or "manual"
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        wallet = conn.execute(
            "SELECT wallet_id, caller_trust FROM wallets WHERE owner_id = %s",
            (normalized_owner_id,),
        ).fetchone()
        if wallet is None:
            conn.execute(
                "INSERT INTO wallets (wallet_id, owner_id, balance_cents, caller_trust, created_at) VALUES (%s, %s, 0, 0.5, %s)",
                (str(uuid.uuid4()), normalized_owner_id, _now()),
            )
            before = 0.5
        else:
            before = float(
                wallet["caller_trust"] if wallet["caller_trust"] is not None else 0.5
            )
        after = max(0.0, min(1.0, before + float(delta)))
        conn.execute(
            "UPDATE wallets SET caller_trust = %s WHERE owner_id = %s",
            (after, normalized_owner_id),
        )
        conn.execute(
            """
            INSERT INTO caller_trust_events
                (event_id, owner_id, delta, before_value, after_value, reason, related_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                normalized_owner_id,
                float(delta),
                before,
                after,
                normalized_reason,
                str(related_id).strip() if related_id else None,
                _now(),
            ),
        )
    return {
        "owner_id": normalized_owner_id,
        "before": before,
        "after": after,
        "delta": float(delta),
    }


def adjust_caller_trust_once(
    owner_id: str,
    *,
    delta: float,
    reason: str,
    related_id: str,
) -> dict:
    """Idempotent version of ``adjust_caller_trust``.

    Uses ``(owner_id, related_id)`` as a uniqueness key — if the same event
    has already been recorded, returns the existing record without inserting.
    """
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM caller_trust_events
            WHERE owner_id = %s AND reason = %s AND related_id = %s
            LIMIT 1
            """,
            (owner_id, reason, related_id),
        ).fetchone()
    if row is not None:
        current = get_caller_trust(owner_id)
        return {"owner_id": owner_id, "before": current, "after": current, "delta": 0.0}
    return adjust_caller_trust(
        owner_id, delta=delta, reason=reason, related_id=related_id
    )


def record_judge_fee(
    platform_wallet_id: str,
    judge_wallet_id: str,
    *,
    charge_tx_id: str,
    agent_id: str,
    fee_cents: int,
) -> None:
    """Transfer the dispute judge fee from the platform wallet to the judge's wallet.

    No-ops if ``fee_cents <= 0``. Runs atomically in a single transaction.
    """
    if fee_cents <= 0:
        return
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT 1 FROM transactions
            WHERE related_tx_id = %s AND wallet_id = %s AND type = 'fee'
            LIMIT 1
            """,
            (f"judge_fee:{charge_tx_id}", judge_wallet_id),
        ).fetchone()
        if existing is not None:
            return
        _debit_wallet_conn(
            conn,
            platform_wallet_id,
            fee_cents,
            agent_id=agent_id,
            related_tx_id=f"judge_fee:{charge_tx_id}",
            memo=f"Quality judge fee for call {charge_tx_id[:8]}",
        )
        _credit_wallet_conn(
            conn,
            judge_wallet_id,
            fee_cents,
            tx_type="fee",
            agent_id=agent_id,
            related_tx_id=f"judge_fee:{charge_tx_id}",
            memo=f"Quality judge fee receipt for call {charge_tx_id[:8]}",
        )


def _get_or_create_wallet_id_conn(conn: _db.DbConnection, owner_id: str) -> str:
    row = conn.execute(
        "SELECT wallet_id FROM wallets WHERE owner_id = %s",
        (owner_id,),
    ).fetchone()
    if row is not None:
        return str(row["wallet_id"])
    wallet_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO wallets (wallet_id, owner_id, balance_cents, caller_trust, created_at)
        VALUES (%s, %s, 0, 0.5, %s)
        """,
        (wallet_id, owner_id, _now()),
    )
    return wallet_id


def _wallet_balance_conn(conn: _db.DbConnection, wallet_id: str) -> int:
    row = conn.execute(
        "SELECT balance_cents FROM wallets WHERE wallet_id = %s",
        (wallet_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Wallet '{wallet_id}' not found.")
    return int(row["balance_cents"])


def _debit_wallet_conn(
    conn: _db.DbConnection,
    wallet_id: str,
    amount_cents: int,
    *,
    agent_id: str | None,
    related_tx_id: str,
    memo: str,
) -> None:
    if amount_cents < 0:
        raise ValueError("amount_cents must be non-negative.")
    if amount_cents == 0:
        return
    updated = conn.execute(
        """
        UPDATE wallets
        SET balance_cents = balance_cents - %s
        WHERE wallet_id = %s AND balance_cents >= %s
        """,
        (amount_cents, wallet_id, amount_cents),
    ).rowcount
    if updated == 0:
        balance = _wallet_balance_conn(conn, wallet_id)
        raise InsufficientBalanceError(balance, amount_cents)
    _insert_tx_only(
        conn,
        wallet_id,
        "charge",
        -amount_cents,
        agent_id,
        related_tx_id,
        memo,
    )


def _credit_wallet_conn(
    conn: _db.DbConnection,
    wallet_id: str,
    amount_cents: int,
    *,
    tx_type: str,
    agent_id: str | None,
    related_tx_id: str,
    memo: str,
) -> None:
    if amount_cents < 0:
        raise ValueError("amount_cents must be non-negative.")
    _insert_tx(
        conn,
        wallet_id,
        tx_type,
        amount_cents,
        agent_id,
        related_tx_id,
        memo,
    )


def _dispute_context_conn(conn: _db.DbConnection, dispute_id: str) -> dict:
    row = conn.execute(
        """
        SELECT
            d.dispute_id,
            d.job_id,
            d.filed_by_owner_id,
            d.side,
            d.filing_deposit_cents,
            d.status AS dispute_status,
            d.outcome AS dispute_outcome,
            j.agent_id,
            j.price_cents,
            j.caller_charge_cents,
            j.fee_bearer_policy,
            j.platform_fee_pct_at_create,
            j.charge_tx_id,
            j.caller_wallet_id,
            j.agent_wallet_id,
            j.platform_wallet_id,
            j.settled_at
        FROM disputes d
        JOIN jobs j ON j.job_id = d.job_id
        WHERE d.dispute_id = %s
        """,
        (dispute_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Dispute '{dispute_id}' not found.")
    # Defensive: jobs created before the wallet-id columns existed (migration 0019
    # added them) may have NULLs. Surface a clear error rather than letting
    # downstream str(None) produce "None" wallet_ids that fail UPDATE silently.
    for required in ("agent_wallet_id", "platform_wallet_id", "caller_wallet_id"):
        if not row.get(required):
            raise ValueError(
                f"Dispute context incomplete: jobs.{required} is NULL for job "
                f"{row.get('job_id')}. This job pre-dates wallet-id tracking and "
                f"cannot be auto-disputed; contact support."
            )
    return row


def _related_sum_conn(
    conn: _db.DbConnection, *, related_tx_id: str, wallet_id: str, tx_type: str
) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS total
        FROM transactions
        WHERE related_tx_id = %s AND wallet_id = %s AND type = %s
        """,
        (related_tx_id, wallet_id, tx_type),
    ).fetchone()
    return int(row["total"] or 0)


def _lock_dispute_funds_conn(conn: _db.DbConnection, dispute_id: str) -> dict:
    """
    Lock dispute funds into escrow.
    If payout already happened, claw back from agent/platform into dispute escrow.
    If payout has not happened yet, charge remains held and no extra movement is needed.

    When the underlying job has an active wallet_holds row (i.e. the
    dispute window hasn't expired and the agent didn't withdraw the
    held slice), the hold is consumed in this same transaction so the
    held_cents cache stays consistent. When no hold exists (pre-deploy
    job OR window expired before the dispute was filed) the existing
    debit path runs unchanged — and will surface InsufficientBalanceError
    upstream if the agent's balance is short, mirroring the historical
    defense-in-depth behaviour.
    """
    ctx = _dispute_context_conn(conn, dispute_id)
    escrow_wallet_id = _get_or_create_wallet_id_conn(
        conn, f"{DISPUTE_ESCROW_OWNER_PREFIX}{dispute_id}"
    )

    already_locked = _related_sum_conn(
        conn,
        related_tx_id=dispute_id,
        wallet_id=escrow_wallet_id,
        tx_type="deposit",
    )
    if already_locked > 0:
        return {
            "dispute_id": dispute_id,
            "escrow_wallet_id": escrow_wallet_id,
            "locked_cents": already_locked,
        }

    charge_tx_id = str(ctx["charge_tx_id"])
    agent_wallet_id = str(ctx["agent_wallet_id"])
    platform_wallet_id = str(ctx["platform_wallet_id"])
    agent_id = str(ctx["agent_id"])
    job_id = str(ctx["job_id"])

    agent_paid = _related_sum_conn(
        conn,
        related_tx_id=charge_tx_id,
        wallet_id=agent_wallet_id,
        tx_type="payout",
    )
    platform_paid = _related_sum_conn(
        conn,
        related_tx_id=charge_tx_id,
        wallet_id=platform_wallet_id,
        tx_type="fee",
    )
    total_locked = agent_paid + platform_paid

    if total_locked > 0:
        # Consume the agent-side hold first (if any). This drops held_cents
        # by the full hold amount in the same transaction as the debit
        # below, so the available_cents = balance - held invariant survives
        # even if a partial-clawback dispute later replays.
        from core import observability as _obs
        from core.payments import holds as _holds

        consumed = _holds.consume_hold_conn(
            conn,
            job_id=job_id,
            clawback_cents=int(agent_paid),
            reason=_holds.RELEASE_REASON_DISPUTE_CLAWBACK,
        )
        if consumed is not None:
            _obs.wallet_hold_released_total.labels(
                reason=_holds.RELEASE_REASON_DISPUTE_CLAWBACK
            ).inc()
            _obs.wallet_hold_clawed_total.labels(
                reason=_holds.RELEASE_REASON_DISPUTE_CLAWBACK
            ).inc()

        _debit_wallet_conn(
            conn,
            agent_wallet_id,
            agent_paid,
            agent_id=agent_id,
            related_tx_id=dispute_id,
            memo=f"Dispute clawback from agent for {dispute_id[:8]}",
        )
        _debit_wallet_conn(
            conn,
            platform_wallet_id,
            platform_paid,
            agent_id=agent_id,
            related_tx_id=dispute_id,
            memo=f"Dispute clawback from platform for {dispute_id[:8]}",
        )
        _credit_wallet_conn(
            conn,
            escrow_wallet_id,
            total_locked,
            tx_type="deposit",
            agent_id=agent_id,
            related_tx_id=dispute_id,
            memo=f"Dispute escrow lock for {dispute_id[:8]}",
        )

    return {
        "dispute_id": dispute_id,
        "escrow_wallet_id": escrow_wallet_id,
        "locked_cents": total_locked,
    }


def lock_dispute_funds(dispute_id: str, conn: _db.DbConnection | None = None) -> dict:
    if conn is not None:
        return _lock_dispute_funds_conn(conn, dispute_id)
    with _conn() as managed_conn:
        managed_conn.execute("BEGIN IMMEDIATE")
        return _lock_dispute_funds_conn(managed_conn, dispute_id)


_DISPUTE_HOLD_ENABLED = bool(
    int(os.environ.get("AZTEA_DISPUTE_HOLD_ENABLED", "0"))
)


def _collect_dispute_filing_deposit_conn(
    conn: _db.DbConnection,
    *,
    dispute_id: str,
    filed_by_owner_id: str,
    amount_cents: int,
) -> dict:
    if amount_cents < 0:
        raise ValueError("amount_cents must be non-negative.")
    deposit_wallet_id = _get_or_create_wallet_id_conn(
        conn, f"{DISPUTE_DEPOSIT_OWNER_PREFIX}{dispute_id}"
    )
    if amount_cents == 0:
        return {
            "dispute_id": dispute_id,
            "deposit_wallet_id": deposit_wallet_id,
            "collected_cents": 0,
        }
    already_collected = _related_sum_conn(
        conn,
        related_tx_id=dispute_id,
        wallet_id=deposit_wallet_id,
        tx_type="deposit",
    )
    if already_collected > 0:
        return {
            "dispute_id": dispute_id,
            "deposit_wallet_id": deposit_wallet_id,
            "collected_cents": already_collected,
        }
    ctx = _dispute_context_conn(conn, dispute_id)
    agent_id = str(ctx["agent_id"])
    filer_wallet_id = _get_or_create_wallet_id_conn(conn, str(filed_by_owner_id))
    _debit_wallet_conn(
        conn,
        filer_wallet_id,
        amount_cents,
        agent_id=agent_id,
        related_tx_id=dispute_id,
        memo=f"Dispute filing deposit for {dispute_id[:8]}",
    )
    _credit_wallet_conn(
        conn,
        deposit_wallet_id,
        amount_cents,
        tx_type="deposit",
        agent_id=agent_id,
        related_tx_id=dispute_id,
        memo=f"Dispute filing deposit escrow for {dispute_id[:8]}",
    )
    # M-4 (audit 2026-05-19): also bump the filer wallet's held_cents
    # column so manage_budget(action='balance') reflects the deposit as
    # "money committed to an open dispute" rather than just disappearing
    # from balance_cents. Existing escrow-by-separation (deposit_wallet)
    # is preserved for atomicity; this just makes the held_cents field
    # honest. Gated behind AZTEA_DISPUTE_HOLD_ENABLED so existing open
    # disputes (which already moved balance without held) don't double-
    # count when the flag flips on. Release path mirrors the increment.
    if _DISPUTE_HOLD_ENABLED:
        try:
            conn.execute(
                "UPDATE wallets SET held_cents = COALESCE(held_cents, 0) + %s "
                "WHERE wallet_id = %s",
                (int(amount_cents), filer_wallet_id),
            )
        except Exception:
            _LOG.exception(
                "Failed to bump filer held_cents for dispute %s; deposit "
                "escrow still collected.", dispute_id,
            )
    return {
        "dispute_id": dispute_id,
        "deposit_wallet_id": deposit_wallet_id,
        "collected_cents": amount_cents,
        "filer_held_cents_bumped": _DISPUTE_HOLD_ENABLED,
    }


def collect_dispute_filing_deposit(
    dispute_id: str,
    *,
    filed_by_owner_id: str,
    amount_cents: int,
    conn: _db.DbConnection | None = None,
) -> dict:
    """Debit the dispute filing deposit from the caller's wallet.

    Pass an open ``conn`` to run within an existing transaction (required for
    atomicity with the dispute insert). Returns the transaction record.
    """
    if conn is not None:
        return _collect_dispute_filing_deposit_conn(
            conn,
            dispute_id=dispute_id,
            filed_by_owner_id=filed_by_owner_id,
            amount_cents=amount_cents,
        )
    with _conn() as managed_conn:
        managed_conn.execute("BEGIN IMMEDIATE")
        return _collect_dispute_filing_deposit_conn(
            managed_conn,
            dispute_id=dispute_id,
            filed_by_owner_id=filed_by_owner_id,
            amount_cents=amount_cents,
        )


def _release_dispute_filing_deposit_conn(
    conn: _db.DbConnection,
    *,
    dispute_id: str,
    outcome: str,
    agent_id: str,
    platform_wallet_id: str,
) -> dict:
    ctx = _dispute_context_conn(conn, dispute_id)
    configured_deposit_cents = int(ctx["filing_deposit_cents"] or 0)
    deposit_wallet_id = _get_or_create_wallet_id_conn(
        conn, f"{DISPUTE_DEPOSIT_OWNER_PREFIX}{dispute_id}"
    )
    if configured_deposit_cents <= 0:
        return {
            "deposit_wallet_id": deposit_wallet_id,
            "filing_deposit_cents": 0,
            "filing_deposit_refunded_cents": 0,
            "filing_deposit_forfeited_cents": 0,
        }
    deposit_balance = _wallet_balance_conn(conn, deposit_wallet_id)
    if deposit_balance <= 0:
        return {
            "deposit_wallet_id": deposit_wallet_id,
            "filing_deposit_cents": configured_deposit_cents,
            "filing_deposit_refunded_cents": 0,
            "filing_deposit_forfeited_cents": 0,
        }
    filed_side = str(ctx["side"] or "").strip().lower()
    filer_owner_id = str(ctx["filed_by_owner_id"] or "").strip()
    filer_won = (filed_side == "caller" and outcome == "caller_wins") or (
        filed_side == "agent" and outcome == "agent_wins"
    )
    refund_to_filer = filer_won or outcome in {"split", "void"}
    if refund_to_filer and filer_owner_id:
        target_wallet_id = _get_or_create_wallet_id_conn(conn, filer_owner_id)
        destination = "filer"
    else:
        target_wallet_id = platform_wallet_id
        destination = "platform"
    _debit_wallet_conn(
        conn,
        deposit_wallet_id,
        deposit_balance,
        agent_id=agent_id,
        related_tx_id=dispute_id,
        memo=f"Dispute filing deposit release for {dispute_id[:8]}",
    )
    _credit_wallet_conn(
        conn,
        target_wallet_id,
        deposit_balance,
        tx_type="deposit",
        agent_id=agent_id,
        related_tx_id=dispute_id,
        memo=f"Dispute filing deposit to {destination} for {dispute_id[:8]}",
    )
    # M-4 (audit 2026-05-19): mirror the held_cents bump done at collect
    # time. Decrement the filer's held_cents by the original configured
    # deposit (not deposit_balance — they can differ if a partial credit
    # already moved). Gated by the same env flag; skipping when the flag
    # was off at collect time avoids over-decrement.
    if _DISPUTE_HOLD_ENABLED and filer_owner_id:
        try:
            filer_wallet_id = _get_or_create_wallet_id_conn(conn, filer_owner_id)
            conn.execute(
                "UPDATE wallets SET held_cents = MAX(0, COALESCE(held_cents, 0) - %s) "
                "WHERE wallet_id = %s",
                (int(configured_deposit_cents), filer_wallet_id),
            )
        except Exception:
            _LOG.exception(
                "Failed to release filer held_cents for dispute %s; "
                "deposit escrow still released to %s.", dispute_id, destination,
            )
    return {
        "deposit_wallet_id": deposit_wallet_id,
        "filing_deposit_cents": configured_deposit_cents,
        "filing_deposit_refunded_cents": deposit_balance if refund_to_filer else 0,
        "filing_deposit_forfeited_cents": 0 if refund_to_filer else deposit_balance,
    }


def post_dispute_settlement(
    dispute_id: str,
    outcome: str,
    split_caller_cents: int | None = None,
    split_agent_cents: int | None = None,
) -> dict:
    """
    Apply final ledger movements for a dispute outcome.
    """
    normalized_outcome = str(outcome or "").strip().lower()
    if normalized_outcome not in {"caller_wins", "agent_wins", "split", "void"}:
        raise ValueError("Invalid dispute outcome.")

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ctx = _dispute_context_conn(conn, dispute_id)
        agent_id = str(ctx["agent_id"])
        price_cents = int(ctx["price_cents"])
        platform_fee_pct = int(
            ctx["platform_fee_pct_at_create"]
            if ctx["platform_fee_pct_at_create"] is not None
            else PLATFORM_FEE_PCT
        )
        fee_bearer_policy = normalize_fee_bearer_policy(ctx["fee_bearer_policy"])
        distribution = compute_success_distribution(
            price_cents,
            platform_fee_pct=platform_fee_pct,
            fee_bearer_policy=fee_bearer_policy,
        )
        caller_charge_cents = int(
            ctx["caller_charge_cents"]
            if ctx["caller_charge_cents"] is not None
            else distribution["caller_charge_cents"]
        )
        charge_tx_id = str(ctx["charge_tx_id"])
        caller_wallet_id = str(ctx["caller_wallet_id"])
        agent_wallet_id = str(ctx["agent_wallet_id"])
        platform_wallet_id = str(ctx["platform_wallet_id"])
        escrow_wallet_id = _get_or_create_wallet_id_conn(
            conn, f"{DISPUTE_ESCROW_OWNER_PREFIX}{dispute_id}"
        )

        # HARDEN-6 (audit 2026-05-20): match by (dispute_id, escrow
        # wallet) without binding to the outcome-specific memo. Pre-fix
        # the query keyed on ``memo = "Dispute final settlement
        # (<outcome>)"`` — a second settlement with a different outcome
        # (admin retry, judge re-vote, race) would NOT find the prior
        # finalization row and would apply a second set of ledger
        # entries on top of the first. Now: any prior finalization
        # against this dispute's escrow wallet short-circuits, and we
        # surface the persisted outcome so callers see the canonical
        # decision rather than the late-arriver they happened to send.
        finalized = conn.execute(
            """
            SELECT memo
            FROM transactions
            WHERE related_tx_id = %s
              AND wallet_id = %s
              AND memo LIKE 'Dispute final settlement (%%'
            LIMIT 1
            """,
            (dispute_id, escrow_wallet_id),
        ).fetchone()
        if finalized is not None:
            persisted_memo = ""
            try:
                persisted_memo = str(finalized["memo"] or "")
            except (KeyError, IndexError, TypeError):
                persisted_memo = str(finalized[0] or "") if finalized else ""
            # Extract "<outcome>" from "Dispute final settlement (<outcome>)".
            _persisted_outcome = normalized_outcome
            _open = persisted_memo.rfind("(")
            _close = persisted_memo.rfind(")")
            if _open != -1 and _close > _open:
                _persisted_outcome = persisted_memo[_open + 1: _close]
            return {
                "dispute_id": dispute_id,
                "outcome": _persisted_outcome,
                "caller_delta_cents": 0,
                "agent_delta_cents": 0,
                "platform_delta_cents": 0,
                "already_finalized": True,
                "requested_outcome": normalized_outcome,
            }

        escrow_balance = _wallet_balance_conn(conn, escrow_wallet_id)
        fee_cents = int(distribution["platform_fee_cents"])
        default_agent_cents = int(distribution["agent_payout_cents"])
        caller_refund_target_cents = caller_charge_cents

        caller_delta = 0
        agent_delta = 0
        platform_delta = 0

        if normalized_outcome == "caller_wins":
            if escrow_balance > 0:
                payout_cents = min(caller_refund_target_cents, escrow_balance)
                _debit_wallet_conn(
                    conn,
                    escrow_wallet_id,
                    payout_cents,
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute release to caller for {dispute_id[:8]}",
                )
                _credit_wallet_conn(
                    conn,
                    caller_wallet_id,
                    payout_cents,
                    tx_type="refund",
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute caller win refund for {dispute_id[:8]}",
                )
                caller_delta += payout_cents
            else:
                _credit_wallet_conn(
                    conn,
                    caller_wallet_id,
                    caller_refund_target_cents,
                    tx_type="refund",
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute caller win refund for {dispute_id[:8]}",
                )
                caller_delta += caller_refund_target_cents

        elif normalized_outcome == "agent_wins":
            if escrow_balance > 0:
                payout_cents = min(default_agent_cents, escrow_balance)
                fee_release_cents = min(
                    fee_cents, max(0, escrow_balance - payout_cents)
                )
                release_total = payout_cents + fee_release_cents
                if release_total > 0:
                    _debit_wallet_conn(
                        conn,
                        escrow_wallet_id,
                        release_total,
                        agent_id=agent_id,
                        related_tx_id=dispute_id,
                        memo=f"Dispute release to agent/platform for {dispute_id[:8]}",
                    )
                if payout_cents > 0:
                    _credit_wallet_conn(
                        conn,
                        agent_wallet_id,
                        payout_cents,
                        tx_type="payout",
                        agent_id=agent_id,
                        related_tx_id=dispute_id,
                        memo=f"Dispute agent win payout for {dispute_id[:8]}",
                    )
                    agent_delta += payout_cents
                if fee_release_cents > 0:
                    _credit_wallet_conn(
                        conn,
                        platform_wallet_id,
                        fee_release_cents,
                        tx_type="fee",
                        agent_id=agent_id,
                        related_tx_id=dispute_id,
                        memo=f"Dispute agent win platform fee for {dispute_id[:8]}",
                    )
                    platform_delta += fee_release_cents
            else:
                _credit_wallet_conn(
                    conn,
                    agent_wallet_id,
                    default_agent_cents,
                    tx_type="payout",
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute agent win payout for {dispute_id[:8]}",
                )
                _credit_wallet_conn(
                    conn,
                    platform_wallet_id,
                    fee_cents,
                    tx_type="fee",
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute agent win platform fee for {dispute_id[:8]}",
                )
                agent_delta += default_agent_cents
                platform_delta += fee_cents

        elif normalized_outcome == "split":
            if split_caller_cents is None or split_agent_cents is None:
                raise ValueError(
                    "split outcomes require split_caller_cents and split_agent_cents."
                )
            caller_share = int(split_caller_cents)
            agent_share = int(split_agent_cents)
            if caller_share < 0 or agent_share < 0:
                raise ValueError("split shares must be non-negative.")
            if caller_share + agent_share > caller_charge_cents:
                raise ValueError("split shares cannot exceed job price.")
            platform_share = caller_charge_cents - caller_share - agent_share

            total_release = caller_share + agent_share + platform_share
            if escrow_balance >= total_release and total_release > 0:
                _debit_wallet_conn(
                    conn,
                    escrow_wallet_id,
                    total_release,
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute split release for {dispute_id[:8]}",
                )

            if caller_share > 0:
                _credit_wallet_conn(
                    conn,
                    caller_wallet_id,
                    caller_share,
                    tx_type="refund",
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute split caller portion for {dispute_id[:8]}",
                )
                caller_delta += caller_share
            if agent_share > 0:
                _credit_wallet_conn(
                    conn,
                    agent_wallet_id,
                    agent_share,
                    tx_type="payout",
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute split agent portion for {dispute_id[:8]}",
                )
                agent_delta += agent_share
            if platform_share > 0:
                _credit_wallet_conn(
                    conn,
                    platform_wallet_id,
                    platform_share,
                    tx_type="fee",
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute split platform portion for {dispute_id[:8]}",
                )
                platform_delta += platform_share

        else:  # void
            if escrow_balance > 0:
                _debit_wallet_conn(
                    conn,
                    escrow_wallet_id,
                    escrow_balance,
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute void release for {dispute_id[:8]}",
                )
                _credit_wallet_conn(
                    conn,
                    caller_wallet_id,
                    escrow_balance,
                    tx_type="refund",
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute void refund for {dispute_id[:8]}",
                )
                caller_delta += escrow_balance

        _insert_tx_only(
            conn,
            escrow_wallet_id,
            "fee",
            0,
            agent_id,
            dispute_id,
            f"Dispute final settlement ({normalized_outcome})",
        )
        filing_deposit_summary = _release_dispute_filing_deposit_conn(
            conn,
            dispute_id=dispute_id,
            outcome=normalized_outcome,
            agent_id=agent_id,
            platform_wallet_id=platform_wallet_id,
        )

        result = {
            "dispute_id": dispute_id,
            "outcome": normalized_outcome,
            "caller_delta_cents": caller_delta,
            "agent_delta_cents": agent_delta,
            "platform_delta_cents": platform_delta,
            "charge_tx_id": charge_tx_id,
            "filing_deposit_cents": int(filing_deposit_summary["filing_deposit_cents"]),
            "filing_deposit_refunded_cents": int(
                filing_deposit_summary["filing_deposit_refunded_cents"]
            ),
            "filing_deposit_forfeited_cents": int(
                filing_deposit_summary["filing_deposit_forfeited_cents"]
            ),
        }
    logging_utils.log_event(
        _LOG,
        logging.INFO,
        "payment.settlement",
        {
            "kind": "dispute_settlement",
            **result,
        },
    )
    return result


def get_settlement_transactions(charge_tx_id: str) -> list:
    """
    Return the charge transaction and any related refund/payout/fee rows linked to it.
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM transactions
            WHERE tx_id = %s OR related_tx_id = %s
            ORDER BY created_at ASC, tx_id ASC
            """,
            (charge_tx_id, charge_tx_id),
        ).fetchall()
    return [dict(row) for row in rows]


def compute_ledger_invariants(max_mismatches: int = 100) -> dict:
    """Verify the three key ledger invariants and return a reconciliation report.

    Invariant 1 — Global balance sum:
        ``SUM(wallets.balance_cents)`` must equal ``SUM(transactions.amount_cents)``.
        ``wallets.balance_cents`` is a denormalised cache; divergence means a
        wallet UPDATE happened without a corresponding ledger INSERT, or vice versa.

    Invariant 2 — Per-wallet balance cache:
        For each wallet, ``balance_cents`` must equal the sum of its transaction
        amounts. Up to ``max_mismatches`` (capped at 1000) drifted wallets are
        returned with the cached vs. computed values.

    Invariant 3 — Per-wallet held cache:
        For each wallet, ``held_cents`` must equal
        ``SUM(wallet_holds.amount_cents WHERE status='active')``. This catches
        bugs in the hold lifecycle (settlement that bumped held without
        inserting the row, missed sweeper release, manual UPDATE bypass).
        Drifted wallets surface in ``held_mismatches`` with the cached vs.
        active sum and the drift.

    Returns ``{wallet_total, ledger_total, drift_cents, mismatch_count,
    mismatches, held_drift_cents, held_mismatch_count, held_mismatches}``.

    Called by ``POST /ops/payments/reconcile``; results are also persisted via
    ``record_reconciliation_run`` for audit history.
    """
    capped = min(max(1, max_mismatches), 1000)
    with _conn() as conn:
        wallet_total = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS total FROM wallets"
        ).fetchone()["total"]
        ledger_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transactions"
        ).fetchone()["total"]
        held_cache_total = conn.execute(
            "SELECT COALESCE(SUM(held_cents), 0) AS total FROM wallets"
        ).fetchone()["total"]
        held_active_total = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM wallet_holds
            WHERE status = 'active'
            """
        ).fetchone()["total"]
        held_mismatches = conn.execute(
            """
            SELECT * FROM (
                SELECT
                    w.wallet_id,
                    w.owner_id,
                    COALESCE(w.held_cents, 0) AS held_cents,
                    COALESCE(SUM(h.amount_cents), 0) AS active_held_cents
                FROM wallets w
                LEFT JOIN wallet_holds h
                  ON h.wallet_id = w.wallet_id AND h.status = 'active'
                GROUP BY w.wallet_id, w.owner_id, w.held_cents
            ) sub
            WHERE held_cents != active_held_cents
            ORDER BY ABS(held_cents - active_held_cents) DESC, wallet_id ASC
            LIMIT %s
            """,
            (capped,),
        ).fetchall()
        # Postgres doesn't allow referencing GROUP BY aggregate aliases in
        # HAVING/ORDER BY by name (only by expression). Wrap in a subquery so
        # the same SQL works on both backends; the SQLite execution plan is
        # unchanged. Bug surfaced as `column "ledger_balance_cents" does not
        # exist` flooding production logs every reconciliation tick.
        mismatches = conn.execute(
            """
            SELECT * FROM (
                SELECT
                    w.wallet_id,
                    w.owner_id,
                    w.balance_cents,
                    COALESCE(SUM(t.amount_cents), 0) AS ledger_balance_cents
                FROM wallets w
                LEFT JOIN transactions t ON t.wallet_id = w.wallet_id
                GROUP BY w.wallet_id, w.owner_id, w.balance_cents
            ) sub
            WHERE balance_cents != ledger_balance_cents
            ORDER BY ABS(balance_cents - ledger_balance_cents) DESC, wallet_id ASC
            LIMIT %s
            """,
            (capped,),
        ).fetchall()
        wallet_count = conn.execute("SELECT COUNT(*) AS count FROM wallets").fetchone()[
            "count"
        ]
        tx_count = conn.execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()["count"]

    drift_cents = int(wallet_total) - int(ledger_total)
    mismatch_rows = [dict(row) for row in mismatches]
    held_mismatch_rows = [dict(row) for row in held_mismatches]
    held_drift_cents = int(held_cache_total) - int(held_active_total)
    invariant_ok = (
        drift_cents == 0
        and len(mismatch_rows) == 0
        and held_drift_cents == 0
        and len(held_mismatch_rows) == 0
    )
    return {
        "invariant_ok": invariant_ok,
        "wallet_total_cents": int(wallet_total),
        "ledger_total_cents": int(ledger_total),
        "drift_cents": int(drift_cents),
        "wallet_count": int(wallet_count),
        "transaction_count": int(tx_count),
        "mismatch_count": len(mismatch_rows),
        "mismatches": mismatch_rows,
        # Reserve-hold pattern (PR #wallet_holds): the held_cents cache
        # mirrors SUM(wallet_holds.amount_cents WHERE status='active').
        # Non-zero drift here indicates a hold lifecycle bug — read
        # docs/runbooks/ledger-drift.md for the diagnosis flow.
        "held_cache_total_cents": int(held_cache_total),
        "held_active_total_cents": int(held_active_total),
        "held_drift_cents": held_drift_cents,
        "held_mismatch_count": len(held_mismatch_rows),
        "held_mismatches": held_mismatch_rows,
    }


def record_reconciliation_run(max_mismatches: int = 100) -> dict:
    """Run reconciliation and persist the result snapshot to the audit log.

    Calls ``compute_ledger_invariants``, inserts a run record, and returns
    the summary dict including any mismatches found.
    """
    summary = compute_ledger_invariants(max_mismatches=max_mismatches)
    run_id = str(uuid.uuid4())
    created_at = _now()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO reconciliation_runs
                (run_id, created_at, invariant_ok, drift_cents, mismatch_count, summary_json)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                created_at,
                1 if summary["invariant_ok"] else 0,
                summary["drift_cents"],
                summary["mismatch_count"],
                json.dumps(summary),
            ),
        )
    return {
        "run_id": run_id,
        "created_at": created_at,
        **summary,
    }


def list_reconciliation_runs(limit: int = 20) -> list:
    """Return the most recent reconciliation run records, newest first (capped at 200)."""
    capped = min(max(1, limit), 200)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT run_id, created_at, invariant_ok, drift_cents, mismatch_count, summary_json
            FROM reconciliation_runs
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (capped,),
        ).fetchall()

    items: list[dict] = []
    for row in rows:
        data = dict(row)
        try:
            summary = json.loads(data.pop("summary_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            summary = {}
        data["invariant_ok"] = bool(data["invariant_ok"])
        data["summary"] = summary if isinstance(summary, dict) else {}
        items.append(data)
    return items
