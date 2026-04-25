"""Helpers for per-agent sub-wallets.

A sub-wallet is any wallet whose ``parent_wallet_id`` is non-NULL. Today the
only sub-wallets created by the platform are agent payout wallets
(``owner_id = 'agent:<agent_id>'``), but the schema is generic.

Phase 1 responsibilities live here:
- enumerate sub-wallets under a parent
- mutate guarantor and label metadata
- sweep funds from a sub-wallet to its parent

Spending enforcement (the ``guarantor_enabled`` / ``guarantor_cap_cents``
behaviour during ``pre_call_charge``) is Phase 2 and intentionally not wired
yet.
"""

from __future__ import annotations

from core.payments.base import (
    InsufficientBalanceError,
    _conn,
    _insert_tx,
)


def list_child_wallets(parent_wallet_id: str) -> list[dict]:
    """Return every wallet whose ``parent_wallet_id`` matches the argument.

    Result rows are ordered by ``created_at`` ascending so the UI shows wallets
    in registration order. Empty list if the parent has no children.
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM wallets
            WHERE parent_wallet_id = ?
            ORDER BY created_at ASC
            """,
            (parent_wallet_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_wallet_guarantor(
    wallet_id: str,
    *,
    enabled: bool,
    cap_cents: int | None,
) -> dict:
    """Set guarantor policy on a sub-wallet.

    ``cap_cents`` may be None (no cap) or a non-negative integer (max parent
    coverage per UTC day, applied by Phase 2 spend logic).
    """
    if cap_cents is not None:
        cap_cents = int(cap_cents)
        if cap_cents < 0:
            raise ValueError("guarantor_cap_cents must be >= 0.")
    with _conn() as conn:
        updated = conn.execute(
            """
            UPDATE wallets
            SET guarantor_enabled = ?, guarantor_cap_cents = ?
            WHERE wallet_id = ?
            """,
            (1 if enabled else 0, cap_cents, wallet_id),
        ).rowcount
        if updated == 0:
            raise ValueError(f"Wallet '{wallet_id}' not found.")
        row = conn.execute(
            "SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()
    return dict(row)


def set_wallet_label(wallet_id: str, display_label: str | None) -> dict:
    """Update the wallet's display_label. Pass None to clear it."""
    label = (display_label or "").strip() or None
    if label is not None and len(label) > 80:
        raise ValueError("display_label must be 80 characters or fewer.")
    with _conn() as conn:
        updated = conn.execute(
            "UPDATE wallets SET display_label = ? WHERE wallet_id = ?",
            (label, wallet_id),
        ).rowcount
        if updated == 0:
            raise ValueError(f"Wallet '{wallet_id}' not found.")
        row = conn.execute(
            "SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()
    return dict(row)


def sweep_to_parent(
    wallet_id: str,
    *,
    amount_cents: int | None = None,
    memo: str = "",
) -> dict:
    """Move funds from a sub-wallet to its parent wallet atomically.

    ``amount_cents=None`` sweeps the full current balance. Sweeping zero is a
    no-op that returns ``{"amount_cents": 0}`` without writing any rows.

    Inserts a ``charge`` on the sub-wallet and a ``deposit`` on the parent,
    linked via ``related_tx_id`` (the parent deposit references the sub-wallet
    charge). Reuses :func:`core.payments.base._insert_tx` so the wallet
    balance cache stays in sync inside the same transaction.
    """
    sweep_memo = memo or "sub-wallet sweep to parent"
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT wallet_id, parent_wallet_id, balance_cents"
                " FROM wallets WHERE wallet_id = ?",
                (wallet_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Wallet '{wallet_id}' not found.")
            parent_wallet_id = row["parent_wallet_id"]
            if not parent_wallet_id:
                raise ValueError(
                    f"Wallet '{wallet_id}' has no parent_wallet_id; cannot sweep."
                )
            current_balance = int(row["balance_cents"] or 0)
            if amount_cents is None:
                sweep_amount = current_balance
            else:
                sweep_amount = int(amount_cents)
                if sweep_amount < 0:
                    raise ValueError("amount_cents must be >= 0.")
            if sweep_amount == 0:
                conn.execute("COMMIT")
                return {
                    "sweep_tx_id": None,
                    "parent_deposit_tx_id": None,
                    "amount_cents": 0,
                }
            if sweep_amount > current_balance:
                raise InsufficientBalanceError(current_balance, sweep_amount)

            charge_tx_id = _insert_tx(
                conn,
                wallet_id=wallet_id,
                tx_type="charge",
                amount_cents=-sweep_amount,
                agent_id=None,
                related_tx_id=None,
                memo=sweep_memo,
            )
            deposit_tx_id = _insert_tx(
                conn,
                wallet_id=parent_wallet_id,
                tx_type="deposit",
                amount_cents=sweep_amount,
                agent_id=None,
                related_tx_id=charge_tx_id,
                memo=sweep_memo,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {
        "sweep_tx_id": charge_tx_id,
        "parent_deposit_tx_id": deposit_tx_id,
        "amount_cents": sweep_amount,
    }


def get_agent_earnings_breakdown_v2(parent_wallet_id: str) -> list[dict]:
    """Per-agent earnings aggregated across all sub-wallets under a parent.

    Returns one row per child wallet (each agent has its own wallet) with:
        agent_id            - parsed from owner_id 'agent:<agent_id>' (NULL if not an agent wallet)
        wallet_id           - the sub-wallet id
        display_label       - optional human label
        current_balance_cents
        total_earned_cents  - SUM of payout transactions to this wallet
        total_spent_cents   - SUM of (charge + fee) transactions on this wallet (positive number)
        call_count          - number of payout transactions
        last_earned_at      - latest payout timestamp
        guarantor_enabled
        guarantor_cap_cents
        daily_spend_limit_cents

    Wallets that have never received a payout still appear with zeros so newly
    registered agents are visible immediately.
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT
                w.wallet_id,
                w.owner_id,
                w.balance_cents,
                w.display_label,
                w.guarantor_enabled,
                w.guarantor_cap_cents,
                w.daily_spend_limit_cents,
                COALESCE(earn.total_earned_cents, 0) AS total_earned_cents,
                COALESCE(earn.call_count, 0)         AS call_count,
                earn.last_earned_at                  AS last_earned_at,
                COALESCE(spend.total_spent_cents, 0) AS total_spent_cents
            FROM wallets w
            LEFT JOIN (
                SELECT wallet_id,
                       SUM(amount_cents) AS total_earned_cents,
                       COUNT(*)          AS call_count,
                       MAX(created_at)   AS last_earned_at
                FROM transactions
                WHERE type = 'payout'
                GROUP BY wallet_id
            ) earn ON earn.wallet_id = w.wallet_id
            LEFT JOIN (
                SELECT wallet_id,
                       SUM(-amount_cents) AS total_spent_cents
                FROM transactions
                WHERE type IN ('charge','fee') AND amount_cents < 0
                GROUP BY wallet_id
            ) spend ON spend.wallet_id = w.wallet_id
            WHERE w.parent_wallet_id = ?
            ORDER BY total_earned_cents DESC, w.created_at ASC
            """,
            (parent_wallet_id,),
        ).fetchall()

    out = []
    for r in rows:
        owner_id = r["owner_id"] or ""
        agent_id = owner_id[len("agent:"):] if owner_id.startswith("agent:") else None
        out.append({
            "agent_id": agent_id,
            "wallet_id": r["wallet_id"],
            "display_label": r["display_label"],
            "current_balance_cents": int(r["balance_cents"] or 0),
            "total_earned_cents": int(r["total_earned_cents"] or 0),
            "total_spent_cents": int(r["total_spent_cents"] or 0),
            "call_count": int(r["call_count"] or 0),
            "last_earned_at": r["last_earned_at"],
            "guarantor_enabled": bool(r["guarantor_enabled"]),
            "guarantor_cap_cents": (
                int(r["guarantor_cap_cents"]) if r["guarantor_cap_cents"] is not None else None
            ),
            "daily_spend_limit_cents": (
                int(r["daily_spend_limit_cents"])
                if r["daily_spend_limit_cents"] is not None
                else None
            ),
        })
    return out
