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
  - WAL mode enabled; thread-local connections.
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "registry.db")
PLATFORM_OWNER_ID = "platform"
PLATFORM_FEE_PCT = 10  # percent

_local = threading.local()


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
    """Return a thread-local SQLite connection with WAL mode."""
    if not getattr(_local, "conn", None):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_payments_db() -> None:
    """Create wallets and transactions tables and indexes if needed."""
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reconciliation_runs (
                run_id          TEXT PRIMARY KEY,
                created_at      TEXT NOT NULL,
                invariant_ok    INTEGER NOT NULL,
                drift_cents     INTEGER NOT NULL,
                mismatch_count  INTEGER NOT NULL,
                summary_json    TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tx_wallet ON transactions(wallet_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wallet_owner ON wallets(owner_id)"
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_related_unique
            ON transactions(related_tx_id, type, wallet_id)
            WHERE related_tx_id IS NOT NULL
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recon_created ON reconciliation_runs(created_at DESC)"
        )
    get_or_create_wallet(PLATFORM_OWNER_ID)


# ---------------------------------------------------------------------------
# Internal ledger primitive
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
    `conn` must be an active connection managed by the caller's `with _conn()` block.
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


def _insert_tx_only(
    conn: sqlite3.Connection,
    wallet_id: str,
    tx_type: str,
    amount_cents: int,
    agent_id: str | None,
    related_tx_id: str | None,
    memo: str,
) -> str:
    """Insert a transaction row without mutating wallet balance (used when balance already updated)."""
    tx_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO transactions
            (tx_id, wallet_id, type, amount_cents, related_tx_id, agent_id, memo, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tx_id, wallet_id, tx_type, amount_cents, related_tx_id, agent_id, memo, _now()),
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
                "INSERT INTO wallets (wallet_id, owner_id, balance_cents, created_at)"
                " VALUES (?, ?, 0, ?)",
                (wallet_id, owner_id, created_at),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM wallets WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            return dict(row)
        return {
            "wallet_id": wallet_id,
            "owner_id": owner_id,
            "balance_cents": 0,
            "created_at": created_at,
        }


def get_wallet(wallet_id: str) -> dict | None:
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
            (wallet_id, min(limit, 100)),  # cap at 100 for safety
        ).fetchall()
    return [dict(r) for r in rows]


def deposit(wallet_id: str, amount_cents: int, memo: str = "manual deposit") -> str:
    """Credit a wallet. Returns tx_id. Raises ValueError for bad inputs."""
    if amount_cents <= 0:
        raise ValueError(f"Deposit amount must be positive, got {amount_cents}¢.")
    if amount_cents > 1_000_000:
        raise ValueError("Single deposit capped at 1,000,000¢ (10,000 USD).")
    with _conn() as conn:
        wallet = conn.execute(
            "SELECT wallet_id FROM wallets WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()
        if wallet is None:
            raise ValueError(f"Wallet '{wallet_id}' not found.")
        return _insert_tx(conn, wallet_id, "deposit", amount_cents, None, None, memo)


# ---------------------------------------------------------------------------
# Call lifecycle
# ---------------------------------------------------------------------------

def pre_call_charge(caller_wallet_id: str, price_cents: int, agent_id: str) -> str:
    """
    Transaction 1 (pre-call): check balance, deduct charge, update cache.
    Returns charge_tx_id. Raises InsufficientBalanceError if underfunded.
    DB lock held only for this short read-check-write sequence.
    """
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT balance_cents FROM wallets WHERE wallet_id = ?",
            (caller_wallet_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Wallet '{caller_wallet_id}' not found.")
        updated = conn.execute(
            """
            UPDATE wallets
            SET balance_cents = balance_cents - ?
            WHERE wallet_id = ? AND balance_cents >= ?
            """,
            (price_cents, caller_wallet_id, price_cents),
        ).rowcount
        if updated == 0:
            raise InsufficientBalanceError(row["balance_cents"], price_cents)
        return _insert_tx_only(
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
        try:
            _insert_tx(
                conn, agent_wallet_id, "payout", agent_cents, agent_id,
                charge_tx_id, f"Payout 90% for call {charge_tx_id[:8]}",
            )
        except sqlite3.IntegrityError:
            pass  # idempotency: payout already recorded
        try:
            _insert_tx(
                conn, platform_wallet_id, "fee", fee_cents, agent_id,
                charge_tx_id, f"Platform fee 10% for call {charge_tx_id[:8]}",
            )
        except sqlite3.IntegrityError:
            pass  # idempotency: fee already recorded


def post_call_refund(
    caller_wallet_id: str,
    charge_tx_id: str,
    price_cents: int,
    agent_id: str,
) -> None:
    """
    Transaction 2b (failure): refund full price to caller.
    Links to original charge via related_tx_id for audit trail.
    """
    with _conn() as conn:
        try:
            _insert_tx(
                conn, caller_wallet_id, "refund", price_cents, agent_id,
                charge_tx_id, f"Refund for failed call {charge_tx_id[:8]}",
            )
        except sqlite3.IntegrityError:
            pass  # idempotency: refund already recorded


def get_settlement_transactions(charge_tx_id: str) -> list:
    """
    Return the charge transaction and any related refund/payout/fee rows linked to it.
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM transactions
            WHERE tx_id = ? OR related_tx_id = ?
            ORDER BY created_at ASC, tx_id ASC
            """,
            (charge_tx_id, charge_tx_id),
        ).fetchall()
    return [dict(row) for row in rows]


def compute_ledger_invariants(max_mismatches: int = 100) -> dict:
    capped = min(max(1, max_mismatches), 1000)
    with _conn() as conn:
        wallet_total = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS total FROM wallets"
        ).fetchone()["total"]
        ledger_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transactions"
        ).fetchone()["total"]
        mismatches = conn.execute(
            """
            SELECT
                w.wallet_id,
                w.owner_id,
                w.balance_cents,
                COALESCE(SUM(t.amount_cents), 0) AS ledger_balance_cents
            FROM wallets w
            LEFT JOIN transactions t ON t.wallet_id = w.wallet_id
            GROUP BY w.wallet_id
            HAVING w.balance_cents != ledger_balance_cents
            ORDER BY ABS(w.balance_cents - ledger_balance_cents) DESC, w.wallet_id ASC
            LIMIT ?
            """,
            (capped,),
        ).fetchall()
        wallet_count = conn.execute(
            "SELECT COUNT(*) AS count FROM wallets"
        ).fetchone()["count"]
        tx_count = conn.execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()["count"]

    drift_cents = int(wallet_total) - int(ledger_total)
    mismatch_rows = [dict(row) for row in mismatches]
    invariant_ok = (drift_cents == 0) and (len(mismatch_rows) == 0)
    return {
        "invariant_ok": invariant_ok,
        "wallet_total_cents": int(wallet_total),
        "ledger_total_cents": int(ledger_total),
        "drift_cents": int(drift_cents),
        "wallet_count": int(wallet_count),
        "transaction_count": int(tx_count),
        "mismatch_count": len(mismatch_rows),
        "mismatches": mismatch_rows,
    }


def record_reconciliation_run(max_mismatches: int = 100) -> dict:
    summary = compute_ledger_invariants(max_mismatches=max_mismatches)
    run_id = str(uuid.uuid4())
    created_at = _now()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO reconciliation_runs
                (run_id, created_at, invariant_ok, drift_cents, mismatch_count, summary_json)
            VALUES (?, ?, ?, ?, ?, ?)
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
    capped = min(max(1, limit), 200)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT run_id, created_at, invariant_ok, drift_cents, mismatch_count, summary_json
            FROM reconciliation_runs
            ORDER BY created_at DESC
            LIMIT ?
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
