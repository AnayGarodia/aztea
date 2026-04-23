"""Zero-sum compensating refunds for variable-pricing overestimates.

Lives alongside ``base`` so ``core.payments.post_call_refund_difference``
continues to resolve via the package ``__init__`` re-export. Split out
of ``base`` only to keep that module under the 1000-line budget.

The forward payment path (``pre_call_charge`` → ``post_call_payout``)
debits the caller and credits the agent + platform based on the
pre-charge estimate. When an agent reports ``billing_units_actual`` that
is lower than what was estimated, this module inserts three compensating
ledger rows in one atomic transaction so the three legs net to zero —
the caller is refunded the overpayment, and the agent + platform each
give back their proportional share. The original charge / payout / fee
rows are never mutated; the ledger stays insert-only.
"""

from __future__ import annotations

import logging
import sqlite3

from core import logging_utils

from .base import _LOG, _conn, _insert_tx


_PRICING_DIFF_MEMO_TAG = "[pricing-diff]"


def post_call_refund_difference(
    caller_wallet_id: str,
    charge_tx_id: str,
    caller_refund_cents: int,
    agent_id: str,
    *,
    agent_wallet_id: str,
    platform_wallet_id: str,
    agent_clawback_cents: int,
    platform_clawback_cents: int,
    memo: str | None = None,
) -> str | None:
    """Insert a zero-sum compensating refund for variable-pricing overestimates.

    Called after a successful call completes and the actual usage turns
    out cheaper than the pre-charge estimate. Up to three new ledger
    rows are inserted in one atomic transaction so the per-charge
    related-tx sum remains zero and total wallet balance is unchanged:

    - ``+refund`` on the caller wallet   (``caller_refund_cents``)
    - ``-refund`` on the agent wallet    (``agent_clawback_cents``)
    - ``-refund`` on the platform wallet (``platform_clawback_cents``)

    All three legs use ``type='refund'`` so the UNIQUE
    ``(related_tx_id, type, wallet_id)`` constraint doesn't collide
    with the original ``payout`` / ``fee`` rows. Negative amount_cents
    signals a clawback — the wallet's ``balance_cents >= 0`` CHECK
    will reject the insert if the wallet is short, rolling the whole
    transaction back.

    The caller is responsible for computing the three amounts from the
    original and actual ``compute_success_distribution`` outputs so that
    ``caller_refund_cents == agent_clawback_cents + platform_clawback_cents``.
    Passing a non-zero-sum split raises ``ValueError`` — this function
    will not silently destroy or create cents.

    Returns the caller-side refund tx_id, or ``None`` if nothing was
    applied (already reconciled, zero amount, caller would be overpaid,
    or the clawback would drive a wallet negative — in that last case
    the whole transaction rolls back and the estimate stays the
    ledger-of-record).

    Integer cents only.
    """
    caller_refund = int(caller_refund_cents)
    agent_clawback = int(agent_clawback_cents)
    platform_clawback = int(platform_clawback_cents)
    if caller_refund < 0 or agent_clawback < 0 or platform_clawback < 0:
        raise ValueError("All pricing-diff amounts must be non-negative.")
    if caller_refund == 0 and agent_clawback == 0 and platform_clawback == 0:
        return None
    if caller_refund != agent_clawback + platform_clawback:
        raise ValueError(
            "Pricing-diff legs must net to zero: "
            f"caller_refund={caller_refund} must equal "
            f"agent_clawback ({agent_clawback}) + platform_clawback ({platform_clawback})."
        )
    normalized_memo = str(memo or "").strip()
    if _PRICING_DIFF_MEMO_TAG not in normalized_memo:
        prefix = f"{_PRICING_DIFF_MEMO_TAG} "
        normalized_memo = prefix + (
            normalized_memo or f"Variable-pricing refund for call {charge_tx_id[:8]}"
        )
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        charge_row = conn.execute(
            """
            SELECT -amount_cents AS debit_cents
            FROM transactions
            WHERE tx_id = ? AND type = 'charge'
            LIMIT 1
            """,
            (charge_tx_id,),
        ).fetchone()
        if charge_row is None:
            raise ValueError(
                f"Original charge tx '{charge_tx_id}' not found; cannot refund difference."
            )
        original_debit = int(charge_row["debit_cents"] or 0)
        # A prior pricing-diff already balanced the books for this charge —
        # don't apply a second one. Dispute refunds are not tagged, so they
        # don't short-circuit this.
        prior_diff_row = conn.execute(
            """
            SELECT 1
            FROM transactions
            WHERE related_tx_id = ?
              AND type = 'refund'
              AND memo LIKE ?
            LIMIT 1
            """,
            (charge_tx_id, f"%{_PRICING_DIFF_MEMO_TAG}%"),
        ).fetchone()
        if prior_diff_row is not None:
            return None
        prior_refunds_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS refunded_cents
            FROM transactions
            WHERE related_tx_id = ? AND type = 'refund'
            """,
            (charge_tx_id,),
        ).fetchone()
        already_refunded = int(prior_refunds_row["refunded_cents"] or 0) if prior_refunds_row else 0
        remaining = max(0, original_debit - already_refunded)
        if remaining <= 0 or caller_refund > remaining:
            return None
        try:
            if agent_clawback > 0:
                _insert_tx(
                    conn,
                    agent_wallet_id,
                    "refund",
                    -agent_clawback,
                    agent_id,
                    charge_tx_id,
                    f"{_PRICING_DIFF_MEMO_TAG} agent clawback for call {charge_tx_id[:8]}",
                )
            if platform_clawback > 0:
                _insert_tx(
                    conn,
                    platform_wallet_id,
                    "refund",
                    -platform_clawback,
                    agent_id,
                    charge_tx_id,
                    f"{_PRICING_DIFF_MEMO_TAG} platform clawback for call {charge_tx_id[:8]}",
                )
            return _insert_tx(
                conn,
                caller_wallet_id,
                "refund",
                caller_refund,
                agent_id,
                charge_tx_id,
                normalized_memo,
            )
        except sqlite3.IntegrityError as exc:
            # In practice this only triggers when the agent or platform
            # balance can't absorb the clawback, or when a concurrent
            # write lost the race. Roll back the entire legs; the
            # pre-charge estimate becomes the settled price.
            logging_utils.log_event(
                _LOG,
                logging.WARNING,
                "payment.settlement_skipped",
                {
                    "kind": "refund_difference",
                    "reason": "integrity_error",
                    "charge_tx_id": charge_tx_id,
                    "agent_id": agent_id,
                    "caller_refund_cents": caller_refund,
                    "agent_clawback_cents": agent_clawback,
                    "platform_clawback_cents": platform_clawback,
                    "error": str(exc),
                },
            )
            return None
