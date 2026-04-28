"""Ledger audit helpers.

These utilities are intentionally narrow: they expose the wallet-balance-cache
invariant directly so tests, ops scripts, and repair tooling can reason about
drift without duplicating SQL in multiple places.
"""

from __future__ import annotations

import sqlite3

from .base import _conn


def _wallet_balance_snapshot_conn(conn: sqlite3.Connection, wallet_id: str) -> dict:
    row = conn.execute(
        """
        SELECT
            w.wallet_id,
            w.owner_id,
            w.balance_cents AS cached_balance_cents,
            COALESCE(SUM(t.amount_cents), 0) AS ledger_balance_cents
        FROM wallets w
        LEFT JOIN transactions t ON t.wallet_id = w.wallet_id
        WHERE w.wallet_id = ?
        GROUP BY w.wallet_id
        """,
        (wallet_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Wallet '{wallet_id}' not found.")
    cached = int(row["cached_balance_cents"] or 0)
    ledger = int(row["ledger_balance_cents"] or 0)
    return {
        "wallet_id": str(row["wallet_id"]),
        "owner_id": str(row["owner_id"]),
        "cached_balance_cents": cached,
        "ledger_balance_cents": ledger,
        "drift_cents": cached - ledger,
        "invariant_ok": cached == ledger,
    }


def get_wallet_balance_snapshot(wallet_id: str) -> dict:
    """Return cached-vs-ledger balance information for one wallet."""
    with _conn() as conn:
        return _wallet_balance_snapshot_conn(conn, wallet_id)


def repair_wallet_balance_cache(wallet_id: str) -> dict:
    """Rewrite one wallet's cached balance from the ledger-derived total.

    This is a repair tool, not a normal write path. We keep it explicit and
    narrow so operator tooling can fix a drifted cache without touching the
    insert-only transaction ledger.
    """
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        snapshot = _wallet_balance_snapshot_conn(conn, wallet_id)
        if snapshot["drift_cents"] != 0:
            conn.execute(
                "UPDATE wallets SET balance_cents = ? WHERE wallet_id = ?",
                (snapshot["ledger_balance_cents"], wallet_id),
            )
        return _wallet_balance_snapshot_conn(conn, wallet_id)

