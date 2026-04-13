"""
payments.py — SQLite-backed payment ledger for the agentmarket platform.

Two tables live in the same registry.db as the agent registry:

  wallets:
    wallet_id (uuid), owner_id (caller identity or "agent:<id>" or "platform"),
    balance_cents (integer cache), created_at

  transactions:
    tx_id (uuid), wallet_id, type (deposit|charge|fee|refund|payout),
    amount_cents (positive = credit, negative = debit), related_tx_id (nullable),
    agent_id (nullable), memo, created_at

Design rules:
  - All amounts are integer cents. No floats, ever.
  - Transactions are insert-only. No UPDATE or DELETE on the transactions table.
  - balance_cents in wallets is a cache, always updated in the same DB transaction
    as the ledger insert that caused it to change.
  - The HTTP call to the downstream agent happens BETWEEN two short DB transactions
    so we never hold a write lock during network I/O.
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "registry.db")

PLATFORM_OWNER_ID = "platform"
PLATFORM_FEE_PCT = 10  # percent


class InsufficientBalanceError(Exception):
    def __init__(self, balance_cents: int, required_cents: int):
        self.balance_cents = balance_cents
        self.required_cents = required_cents
        super().__init__(
            f"Insufficient balance: have {balance_cents}¢, need {required_cents}¢"
        )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    """Open a new SQLite connection for the calling thread."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_payments_db() -> None:
    """Create wallets and transactions tables if they do not already exist."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                wallet_id     TEXT PRIMARY KEY,
                owner_id      TEXT NOT NULL UNIQUE,
                balance_cents INTEGER NOT NULL DEFAULT 0 CHECK(balance_cents >= 0),
                created_at    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                tx_id         TEXT PRIMARY KEY,
                wallet_id     TEXT NOT NULL,
                type          TEXT NOT NULL CHECK(type IN ('deposit','charge','fee','refund','payout')),
                amount_cents  INTEGER NOT NULL,
                related_tx_id TEXT,
                agent_id      TEXT,
                memo          TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL
            )
        """)
    # Ensure the platform wallet always exists.
    get_or_create_wallet(PLATFORM_OWNER_ID)


# ---------------------------------------------------------------------------
# Internal ledger primitive — always called inside an open `with _conn()` block
# ---------------------------------------------------------------------------

def _insert_tx(
    conn: sqlite3.Connection,
    wallet_id: str,
    tx_type: str,
    amount_cents: int,
    agent_id: str | None,
    related_tx_id: str | None,
    memo: str,
) -> str:
    """
    Insert one transaction row and update the wallet balance cache atomically.
    `conn` must be an active connection whose transaction will be committed by
    the caller's `with _conn() as conn:` block.
    Returns the new tx_id.
    """
    tx_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO transactions
            (tx_id, wallet_id, type, amount_cents, related_tx_id, agent_id, memo, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tx_id, wallet_id, tx_type, amount_cents, related_tx_id, agent_id, memo, _now()),
    )
    conn.execute(
        "UPDATE wallets SET balance_cents = balance_cents + ? WHERE wallet_id = ?",
        (amount_cents, wallet_id),
    )
    return tx_id


# ---------------------------------------------------------------------------
# Wallet management
# ---------------------------------------------------------------------------

def get_or_create_wallet(owner_id: str) -> dict:
    """
    Return the existing wallet for owner_id, or create one with 0 balance.
    Safe to call on every request — creation is idempotent via UNIQUE constraint.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM wallets WHERE owner_id = ?", (owner_id,)
        ).fetchone()
        if row:
            return dict(row)
        wallet_id = str(uuid.uuid4())
        created_at = _now()
        try:
            conn.execute(
                "INSERT INTO wallets (wallet_id, owner_id, balance_cents, created_at) VALUES (?, ?, 0, ?)",
                (wallet_id, owner_id, created_at),
            )
        except sqlite3.IntegrityError:
            # Lost a race with another thread creating the same wallet.
            row = conn.execute(
                "SELECT * FROM wallets WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            return dict(row)
        return {"wallet_id": wallet_id, "owner_id": owner_id, "balance_cents": 0, "created_at": created_at}


def get_wallet(wallet_id: str) -> dict | None:
    """Return wallet dict or None if not found."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()
    return dict(row) if row else None


def get_wallet_transactions(wallet_id: str, limit: int = 20) -> list:
    """Return the most recent `limit` transactions for a wallet, newest first."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM transactions
            WHERE wallet_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (wallet_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def deposit(wallet_id: str, amount_cents: int, memo: str = "manual deposit") -> str:
    """
    Credit a wallet. Returns tx_id.
    Raises ValueError for bad inputs.
    """
    if amount_cents <= 0:
        raise ValueError(f"Deposit amount must be positive, got {amount_cents}¢.")
    with _conn() as conn:
        wallet = conn.execute(
            "SELECT wallet_id FROM wallets WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()
        if wallet is None:
            raise ValueError(f"Wallet '{wallet_id}' not found.")
        return _insert_tx(conn, wallet_id, "deposit", amount_cents, None, None, memo)


# ---------------------------------------------------------------------------
# Call lifecycle — two short transactions bracketing the HTTP call
# ---------------------------------------------------------------------------

def pre_call_charge(caller_wallet_id: str, price_cents: int, agent_id: str) -> str:
    """
    Transaction 1 (pre-call): check balance, deduct charge, update cache.

    Returns charge_tx_id.
    Raises InsufficientBalanceError if balance < price_cents.
    The DB lock is held only for this short read-check-write sequence.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT balance_cents FROM wallets WHERE wallet_id = ?",
            (caller_wallet_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Wallet '{caller_wallet_id}' not found.")
        if row["balance_cents"] < price_cents:
            raise InsufficientBalanceError(row["balance_cents"], price_cents)
        return _insert_tx(
            conn,
            caller_wallet_id,
            "charge",
            -price_cents,
            agent_id,
            None,
            f"Call to agent {agent_id}",
        )


def post_call_payout(
    agent_wallet_id: str,
    platform_wallet_id: str,
    charge_tx_id: str,
    price_cents: int,
    agent_id: str,
) -> None:
    """
    Transaction 2a (success): credit 90% to agent, 10% to platform.
    Both inserts happen in one atomic transaction.
    Fee rounds down; agent gets the remainder to avoid creating or destroying cents.
    """
    fee_cents = price_cents * PLATFORM_FEE_PCT // 100
    agent_cents = price_cents - fee_cents

    with _conn() as conn:
        _insert_tx(
            conn,
            agent_wallet_id,
            "payout",
            agent_cents,
            agent_id,
            charge_tx_id,
            f"Payout 90% for call {charge_tx_id[:8]}",
        )
        _insert_tx(
            conn,
            platform_wallet_id,
            "fee",
            fee_cents,
            agent_id,
            charge_tx_id,
            f"Platform fee 10% for call {charge_tx_id[:8]}",
        )


def post_call_refund(
    caller_wallet_id: str,
    charge_tx_id: str,
    price_cents: int,
    agent_id: str,
) -> None:
    """
    Transaction 2b (failure): refund full price back to caller.
    Links to the original charge via related_tx_id for audit trail.
    """
    with _conn() as conn:
        _insert_tx(
            conn,
            caller_wallet_id,
            "refund",
            price_cents,
            agent_id,
            charge_tx_id,
            f"Refund for failed call {charge_tx_id[:8]}",
        )
