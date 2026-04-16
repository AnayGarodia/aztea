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
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from core import logging_utils
from core import db as _db

DB_PATH = _db.DB_PATH
_local = _db._local
PLATFORM_OWNER_ID = "platform"
DISPUTE_ESCROW_OWNER_PREFIX = "dispute_escrow:"
DISPUTE_RETURN_PLATFORM_FEE_ON_CALLER_WINS = True

_LOG = logging.getLogger(__name__)


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


def _env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer, got {raw!r}.") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}.")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be <= {maximum}, got {value}.")
    return value


PLATFORM_FEE_PCT = _env_int("PLATFORM_FEE_PCT", 10, minimum=0, maximum=100)

def _conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL mode."""
    return _db.get_raw_connection(DB_PATH)


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
                caller_trust  REAL NOT NULL DEFAULT 0.5,
                created_at    TEXT NOT NULL
            )
        """)
        wallet_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(wallets)").fetchall()
        }
        if "caller_trust" not in wallet_cols:
            conn.execute("ALTER TABLE wallets ADD COLUMN caller_trust REAL NOT NULL DEFAULT 0.5")
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS caller_trust_events (
                event_id      TEXT PRIMARY KEY,
                owner_id      TEXT NOT NULL,
                delta         REAL NOT NULL,
                before_value  REAL NOT NULL,
                after_value   REAL NOT NULL,
                reason        TEXT NOT NULL,
                related_id    TEXT,
                created_at    TEXT NOT NULL
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_caller_trust_events_owner_created ON caller_trust_events(owner_id, created_at DESC)"
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
                "INSERT INTO wallets (wallet_id, owner_id, balance_cents, caller_trust, created_at)"
                " VALUES (?, ?, 0, 0.5, ?)",
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
            "caller_trust": 0.5,
            "created_at": created_at,
        }


def get_wallet(wallet_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()
    return dict(row) if row else None


def get_wallet_by_owner(owner_id: str) -> dict | None:
    """Look up a wallet by owner_id (user_id or 'platform')."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM wallets WHERE owner_id = ?", (owner_id,)
        ).fetchone()
    return dict(row) if row else None


def charge(wallet_id: str, amount_cents: int, memo: str = "") -> str:
    """Deduct amount_cents from wallet (e.g. for withdrawal). Returns tx_id.
    Raises InsufficientBalanceError if balance is too low."""
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive.")
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT balance_cents FROM wallets WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Wallet '{wallet_id}' not found.")
        updated = conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents - ? WHERE wallet_id = ? AND balance_cents >= ?",
            (amount_cents, wallet_id, amount_cents),
        ).rowcount
        if updated == 0:
            raise InsufficientBalanceError(row["balance_cents"], amount_cents)
        return _insert_tx_only(conn, wallet_id, "charge", -amount_cents, None, None, memo or "Withdrawal")


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


def get_agent_earnings_breakdown(wallet_id: str) -> list[dict]:
    """
    Return per-agent earnings for a wallet.

    Each row: { agent_id, total_earned_cents, call_count, last_earned_at }
    Only includes payout transactions (i.e. earnings from agent calls).
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT
                agent_id,
                SUM(amount_cents)  AS total_earned_cents,
                COUNT(*)           AS call_count,
                MAX(created_at)    AS last_earned_at
            FROM transactions
            WHERE wallet_id = ?
              AND type = 'payout'
              AND agent_id IS NOT NULL
            GROUP BY agent_id
            ORDER BY total_earned_cents DESC
            """,
            (wallet_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_connect_withdrawals(wallet_id: str, limit: int = 20) -> list[dict]:
    """Return Stripe Connect withdrawals for a wallet, newest first."""
    capped = min(max(1, limit), 200)
    with _conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT transfer_id, wallet_id, amount_cents, stripe_tx_id, memo, created_at
                FROM stripe_connect_transfers
                WHERE wallet_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (wallet_id, capped),
            ).fetchall()
        except sqlite3.OperationalError:
            # Older databases without the Stripe Connect migration should
            # degrade to an empty history instead of failing wallet views.
            return []

    items: list[dict] = []
    for row in rows:
        item = dict(row)
        item["status"] = "complete"
        items.append(item)
    return items


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
    if price_cents < 0:
        raise ValueError("price_cents must be non-negative.")
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
    if price_cents < 0:
        raise ValueError("price_cents must be non-negative.")
    fee_cents = price_cents * PLATFORM_FEE_PCT // 100
    agent_cents = price_cents - fee_cents

    applied = False
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        refund_exists = conn.execute(
            """
            SELECT 1
            FROM transactions
            WHERE related_tx_id = ? AND type = 'refund'
            LIMIT 1
            """,
            (charge_tx_id,),
        ).fetchone()
        if refund_exists is not None:
            logging_utils.log_event(
                _LOG,
                logging.INFO,
                "payment.settlement_skipped",
                {
                    "kind": "payout",
                    "reason": "refund_already_exists",
                    "charge_tx_id": charge_tx_id,
                    "agent_id": agent_id,
                },
            )
            return
        try:
            _insert_tx(
                conn, agent_wallet_id, "payout", agent_cents, agent_id,
                charge_tx_id, f"Payout 90% for call {charge_tx_id[:8]}",
            )
            applied = True
        except sqlite3.IntegrityError:
            pass  # idempotency: payout already recorded
        try:
            _insert_tx(
                conn, platform_wallet_id, "fee", fee_cents, agent_id,
                charge_tx_id, f"Platform fee 10% for call {charge_tx_id[:8]}",
            )
            applied = True
        except sqlite3.IntegrityError:
            pass  # idempotency: fee already recorded
    logging_utils.log_event(
        _LOG,
        logging.INFO,
        "payment.settlement",
        {
            "kind": "payout",
            "charge_tx_id": charge_tx_id,
            "agent_id": agent_id,
            "price_cents": price_cents,
            "agent_payout_cents": agent_cents,
            "platform_fee_cents": fee_cents,
            "applied": applied,
        },
    )


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
    if price_cents < 0:
        raise ValueError("price_cents must be non-negative.")
    applied = False
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        payout_exists = conn.execute(
            """
            SELECT 1
            FROM transactions
            WHERE related_tx_id = ? AND type IN ('payout', 'fee')
            LIMIT 1
            """,
            (charge_tx_id,),
        ).fetchone()
        if payout_exists is not None:
            logging_utils.log_event(
                _LOG,
                logging.INFO,
                "payment.settlement_skipped",
                {
                    "kind": "refund",
                    "reason": "payout_already_exists",
                    "charge_tx_id": charge_tx_id,
                    "agent_id": agent_id,
                },
            )
            return
        try:
            _insert_tx(
                conn, caller_wallet_id, "refund", price_cents, agent_id,
                charge_tx_id, f"Refund for failed call {charge_tx_id[:8]}",
            )
            applied = True
        except sqlite3.IntegrityError:
            pass  # idempotency: refund already recorded
    logging_utils.log_event(
        _LOG,
        logging.INFO,
        "payment.settlement",
        {
            "kind": "refund",
            "charge_tx_id": charge_tx_id,
            "agent_id": agent_id,
            "refund_cents": price_cents,
            "applied": applied,
        },
    )


def post_call_partial_settle(
    caller_wallet_id: str,
    agent_wallet_id: str,
    platform_wallet_id: str,
    charge_tx_id: str,
    price_cents: int,
    refund_fraction: float,
    agent_id: str,
) -> None:
    """
    Partial settlement: refund a fraction of the charge to the caller and
    pay the remainder to the agent (90%) + platform (10%).

    Used when an agent fails after spending some compute — e.g., bad input
    validation that consumed tokens, or partial work before a downstream error.

    refund_fraction=1.0  →  full refund (identical to post_call_refund)
    refund_fraction=0.0  →  full payout to agent (identical to post_call_payout)
    """
    refund_fraction = max(0.0, min(1.0, float(refund_fraction)))
    refund_cents = int(price_cents * refund_fraction)
    kept_cents = price_cents - refund_cents

    fee_cents = kept_cents * PLATFORM_FEE_PCT // 100
    agent_cents = kept_cents - fee_cents

    applied = False
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Idempotency: skip if any settlement already recorded for this charge
        already = conn.execute(
            "SELECT 1 FROM transactions WHERE related_tx_id = ? LIMIT 1",
            (charge_tx_id,),
        ).fetchone()
        if already is not None:
            return
        try:
            if refund_cents > 0:
                _insert_tx(
                    conn, caller_wallet_id, "refund", refund_cents, agent_id,
                    charge_tx_id,
                    f"Partial refund ({int(refund_fraction*100)}%) for call {charge_tx_id[:8]}",
                )
                applied = True
            if agent_cents > 0:
                _insert_tx(
                    conn, agent_wallet_id, "payout", agent_cents, agent_id,
                    charge_tx_id,
                    f"Partial payout ({int((1-refund_fraction)*100)}%) for call {charge_tx_id[:8]}",
                )
                applied = True
            if fee_cents > 0:
                _insert_tx(
                    conn, platform_wallet_id, "fee", fee_cents, agent_id,
                    charge_tx_id,
                    f"Platform fee for partial call {charge_tx_id[:8]}",
                )
                applied = True
        except sqlite3.IntegrityError:
            pass  # idempotency: already recorded

    logging_utils.log_event(
        _LOG,
        logging.INFO,
        "payment.settlement",
        {
            "kind": "partial_settle",
            "charge_tx_id": charge_tx_id,
            "agent_id": agent_id,
            "price_cents": price_cents,
            "refund_fraction": refund_fraction,
            "refund_cents": refund_cents,
            "agent_payout_cents": agent_cents,
            "platform_fee_cents": fee_cents,
            "applied": applied,
        },
    )


def get_caller_trust(owner_id: str) -> float:
    wallet = get_or_create_wallet(owner_id)
    try:
        value = float(wallet.get("caller_trust", 0.5))
    except (TypeError, ValueError):
        value = 0.5
    return max(0.0, min(1.0, value))


def adjust_caller_trust(owner_id: str, *, delta: float, reason: str, related_id: str | None = None) -> dict:
    normalized_owner_id = str(owner_id or "").strip()
    if not normalized_owner_id:
        raise ValueError("owner_id must be a non-empty string.")
    normalized_reason = str(reason or "").strip() or "manual"
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        wallet = conn.execute(
            "SELECT wallet_id, caller_trust FROM wallets WHERE owner_id = ?",
            (normalized_owner_id,),
        ).fetchone()
        if wallet is None:
            conn.execute(
                "INSERT INTO wallets (wallet_id, owner_id, balance_cents, caller_trust, created_at) VALUES (?, ?, 0, 0.5, ?)",
                (str(uuid.uuid4()), normalized_owner_id, _now()),
            )
            before = 0.5
        else:
            before = float(wallet["caller_trust"] if wallet["caller_trust"] is not None else 0.5)
        after = max(0.0, min(1.0, before + float(delta)))
        conn.execute(
            "UPDATE wallets SET caller_trust = ? WHERE owner_id = ?",
            (after, normalized_owner_id),
        )
        conn.execute(
            """
            INSERT INTO caller_trust_events
                (event_id, owner_id, delta, before_value, after_value, reason, related_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
    return {"owner_id": normalized_owner_id, "before": before, "after": after, "delta": float(delta)}


def adjust_caller_trust_once(
    owner_id: str,
    *,
    delta: float,
    reason: str,
    related_id: str,
) -> dict:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM caller_trust_events
            WHERE owner_id = ? AND reason = ? AND related_id = ?
            LIMIT 1
            """,
            (owner_id, reason, related_id),
        ).fetchone()
    if row is not None:
        current = get_caller_trust(owner_id)
        return {"owner_id": owner_id, "before": current, "after": current, "delta": 0.0}
    return adjust_caller_trust(owner_id, delta=delta, reason=reason, related_id=related_id)


def record_judge_fee(
    platform_wallet_id: str,
    judge_wallet_id: str,
    *,
    charge_tx_id: str,
    agent_id: str,
    fee_cents: int,
) -> None:
    if fee_cents <= 0:
        return
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT 1 FROM transactions
            WHERE related_tx_id = ? AND wallet_id = ? AND type = 'fee'
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


def _get_or_create_wallet_id_conn(conn: sqlite3.Connection, owner_id: str) -> str:
    row = conn.execute(
        "SELECT wallet_id FROM wallets WHERE owner_id = ?",
        (owner_id,),
    ).fetchone()
    if row is not None:
        return str(row["wallet_id"])
    wallet_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO wallets (wallet_id, owner_id, balance_cents, caller_trust, created_at)
        VALUES (?, ?, 0, 0.5, ?)
        """,
        (wallet_id, owner_id, _now()),
    )
    return wallet_id


def _wallet_balance_conn(conn: sqlite3.Connection, wallet_id: str) -> int:
    row = conn.execute(
        "SELECT balance_cents FROM wallets WHERE wallet_id = ?",
        (wallet_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Wallet '{wallet_id}' not found.")
    return int(row["balance_cents"])


def _debit_wallet_conn(
    conn: sqlite3.Connection,
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
        SET balance_cents = balance_cents - ?
        WHERE wallet_id = ? AND balance_cents >= ?
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
    conn: sqlite3.Connection,
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


def _dispute_context_conn(conn: sqlite3.Connection, dispute_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            d.dispute_id,
            d.job_id,
            d.status AS dispute_status,
            d.outcome AS dispute_outcome,
            j.agent_id,
            j.price_cents,
            j.charge_tx_id,
            j.caller_wallet_id,
            j.agent_wallet_id,
            j.platform_wallet_id,
            j.settled_at
        FROM disputes d
        JOIN jobs j ON j.job_id = d.job_id
        WHERE d.dispute_id = ?
        """,
        (dispute_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Dispute '{dispute_id}' not found.")
    return row


def _related_sum_conn(conn: sqlite3.Connection, *, related_tx_id: str, wallet_id: str, tx_type: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS total
        FROM transactions
        WHERE related_tx_id = ? AND wallet_id = ? AND type = ?
        """,
        (related_tx_id, wallet_id, tx_type),
    ).fetchone()
    return int(row["total"] or 0)


def _lock_dispute_funds_conn(conn: sqlite3.Connection, dispute_id: str) -> dict:
    """
    Lock dispute funds into escrow.
    If payout already happened, claw back from agent/platform into dispute escrow.
    If payout has not happened yet, charge remains held and no extra movement is needed.
    """
    ctx = _dispute_context_conn(conn, dispute_id)
    escrow_wallet_id = _get_or_create_wallet_id_conn(conn, f"{DISPUTE_ESCROW_OWNER_PREFIX}{dispute_id}")

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


def lock_dispute_funds(dispute_id: str, conn: sqlite3.Connection | None = None) -> dict:
    if conn is not None:
        return _lock_dispute_funds_conn(conn, dispute_id)
    with _conn() as managed_conn:
        managed_conn.execute("BEGIN IMMEDIATE")
        return _lock_dispute_funds_conn(managed_conn, dispute_id)


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
        charge_tx_id = str(ctx["charge_tx_id"])
        caller_wallet_id = str(ctx["caller_wallet_id"])
        agent_wallet_id = str(ctx["agent_wallet_id"])
        platform_wallet_id = str(ctx["platform_wallet_id"])
        escrow_wallet_id = _get_or_create_wallet_id_conn(conn, f"{DISPUTE_ESCROW_OWNER_PREFIX}{dispute_id}")

        finalized = conn.execute(
            """
            SELECT 1
            FROM transactions
            WHERE related_tx_id = ? AND wallet_id = ? AND memo = ?
            LIMIT 1
            """,
            (dispute_id, escrow_wallet_id, f"Dispute final settlement ({normalized_outcome})"),
        ).fetchone()
        if finalized is not None:
            return {
                "dispute_id": dispute_id,
                "outcome": normalized_outcome,
                "caller_delta_cents": 0,
                "agent_delta_cents": 0,
                "platform_delta_cents": 0,
            }

        escrow_balance = _wallet_balance_conn(conn, escrow_wallet_id)
        fee_cents = price_cents * PLATFORM_FEE_PCT // 100
        default_agent_cents = price_cents - fee_cents

        caller_delta = 0
        agent_delta = 0
        platform_delta = 0

        if normalized_outcome == "caller_wins":
            if escrow_balance > 0:
                payout_cents = min(price_cents, escrow_balance)
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
                    price_cents,
                    tx_type="refund",
                    agent_id=agent_id,
                    related_tx_id=dispute_id,
                    memo=f"Dispute caller win refund for {dispute_id[:8]}",
                )
                caller_delta += price_cents

        elif normalized_outcome == "agent_wins":
            if escrow_balance > 0:
                payout_cents = min(default_agent_cents, escrow_balance)
                fee_release_cents = min(fee_cents, max(0, escrow_balance - payout_cents))
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
                raise ValueError("split outcomes require split_caller_cents and split_agent_cents.")
            caller_share = int(split_caller_cents)
            agent_share = int(split_agent_cents)
            if caller_share < 0 or agent_share < 0:
                raise ValueError("split shares must be non-negative.")
            if caller_share + agent_share > price_cents:
                raise ValueError("split shares cannot exceed job price.")
            platform_share = price_cents - caller_share - agent_share

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

        result = {
            "dispute_id": dispute_id,
            "outcome": normalized_outcome,
            "caller_delta_cents": caller_delta,
            "agent_delta_cents": agent_delta,
            "platform_delta_cents": platform_delta,
            "charge_tx_id": charge_tx_id,
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
