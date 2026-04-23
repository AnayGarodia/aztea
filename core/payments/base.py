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
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from core import logging_utils
from core import db as _db

DB_PATH = _db.DB_PATH
_local = _db._local


def _resolved_db_path() -> str:
    """Prefer ``core.payments.DB_PATH`` so isolated tests can monkeypatch the package."""
    pkg = sys.modules.get("core.payments")
    if pkg is not None:
        candidate = getattr(pkg, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return DB_PATH
PLATFORM_OWNER_ID = "platform"
DISPUTE_ESCROW_OWNER_PREFIX = "dispute_escrow:"
DISPUTE_DEPOSIT_OWNER_PREFIX = "dispute_deposit:"
DISPUTE_RETURN_PLATFORM_FEE_ON_CALLER_WINS = True

_LOG = logging.getLogger(__name__)


class InsufficientBalanceError(Exception):
    def __init__(self, balance_cents: int, required_cents: int):
        self.balance_cents = balance_cents
        self.required_cents = required_cents
        super().__init__(
            f"Insufficient balance: have {balance_cents}¢, need {required_cents}¢"
        )


class KeySpendLimitExceededError(Exception):
    def __init__(self, limit_cents: int, spent_cents: int, attempted_cents: int):
        self.limit_cents = int(limit_cents)
        self.spent_cents = int(spent_cents)
        self.attempted_cents = int(attempted_cents)
        super().__init__(
            f"API key spend cap exceeded: spent {spent_cents}¢, attempted {attempted_cents}¢, cap {limit_cents}¢"
        )


class WalletDailySpendLimitExceededError(Exception):
    def __init__(self, limit_cents: int, spent_last_24h_cents: int, attempted_cents: int):
        self.limit_cents = int(limit_cents)
        self.spent_last_24h_cents = int(spent_last_24h_cents)
        self.attempted_cents = int(attempted_cents)
        super().__init__(
            "Wallet daily spend cap exceeded: "
            f"spent_last_24h {spent_last_24h_cents}¢, attempted {attempted_cents}¢, cap {limit_cents}¢"
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
VALID_FEE_BEARER_POLICIES = {"worker", "caller", "split"}
DEFAULT_FEE_BEARER_POLICY = "caller"


def normalize_fee_bearer_policy(value: str | None) -> str:
    normalized = str(value or "").strip().lower() or DEFAULT_FEE_BEARER_POLICY
    if normalized not in VALID_FEE_BEARER_POLICIES:
        return DEFAULT_FEE_BEARER_POLICY
    return normalized


def compute_platform_fee_cents(price_cents: int, platform_fee_pct: int | None = None) -> int:
    pct = PLATFORM_FEE_PCT if platform_fee_pct is None else int(platform_fee_pct)
    if price_cents < 0:
        raise ValueError("price_cents must be non-negative.")
    if pct < 0:
        raise ValueError("platform_fee_pct must be non-negative.")
    fee = (
        Decimal(price_cents) * Decimal(pct) / Decimal(100)
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(fee)


def compute_success_distribution(
    price_cents: int,
    *,
    platform_fee_pct: int | None = None,
    fee_bearer_policy: str | None = None,
) -> dict[str, int]:
    if price_cents < 0:
        raise ValueError("price_cents must be non-negative.")
    fee_cents = compute_platform_fee_cents(price_cents, platform_fee_pct)
    policy = normalize_fee_bearer_policy(fee_bearer_policy)
    if policy == "worker":
        caller_charge_cents = price_cents
        agent_payout_cents = max(0, price_cents - fee_cents)
    elif policy == "split":
        caller_fee_cents = (fee_cents + 1) // 2
        worker_fee_cents = fee_cents - caller_fee_cents
        caller_charge_cents = price_cents + caller_fee_cents
        agent_payout_cents = max(0, price_cents - worker_fee_cents)
    else:  # caller
        caller_charge_cents = price_cents + fee_cents
        agent_payout_cents = price_cents
    return {
        "caller_charge_cents": int(caller_charge_cents),
        "agent_payout_cents": int(agent_payout_cents),
        "platform_fee_cents": int(fee_cents),
    }

def _conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL mode."""
    return _db.get_raw_connection(_resolved_db_path())


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
                daily_spend_limit_cents INTEGER CHECK(daily_spend_limit_cents >= 0),
                created_at    TEXT NOT NULL
            )
        """)
        wallet_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(wallets)").fetchall()
        }
        if "caller_trust" not in wallet_cols:
            conn.execute("ALTER TABLE wallets ADD COLUMN caller_trust REAL NOT NULL DEFAULT 0.5")
        if "daily_spend_limit_cents" not in wallet_cols:
            conn.execute("ALTER TABLE wallets ADD COLUMN daily_spend_limit_cents INTEGER")
        conn.execute(
            """
            UPDATE wallets
            SET daily_spend_limit_cents = NULL
            WHERE daily_spend_limit_cents < 0
            """
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                tx_id         TEXT PRIMARY KEY,
                wallet_id     TEXT NOT NULL,
                type          TEXT NOT NULL CHECK(type IN ('deposit','charge','fee','refund','payout')),
                amount_cents  INTEGER NOT NULL,
                related_tx_id TEXT,
                agent_id      TEXT,
                charged_by_key_id TEXT,
                memo          TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL
            )
        """)
        tx_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
        }
        if "charged_by_key_id" not in tx_cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN charged_by_key_id TEXT")
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
            "CREATE INDEX IF NOT EXISTS idx_tx_wallet_key_type ON transactions(wallet_id, charged_by_key_id, type)"
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
    charged_by_key_id: str | None = None,
) -> str:
    """
    Insert one transaction row and update the wallet balance cache atomically.
    `conn` must be an active connection managed by the caller's `with _conn()` block.
    Returns the new tx_id.
    """
    tx_id = str(uuid.uuid4())
    normalized_key_id = _resolve_charged_by_key_id(conn, charged_by_key_id, related_tx_id)
    conn.execute(
        """
        INSERT INTO transactions
            (tx_id, wallet_id, type, amount_cents, related_tx_id, agent_id, charged_by_key_id, memo, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tx_id,
            wallet_id,
            tx_type,
            amount_cents,
            related_tx_id,
            agent_id,
            normalized_key_id,
            memo,
            _now(),
        ),
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
    charged_by_key_id: str | None = None,
) -> str:
    """Insert a transaction row without mutating wallet balance (used when balance already updated)."""
    tx_id = str(uuid.uuid4())
    normalized_key_id = _resolve_charged_by_key_id(conn, charged_by_key_id, related_tx_id)
    conn.execute(
        """
        INSERT INTO transactions
            (tx_id, wallet_id, type, amount_cents, related_tx_id, agent_id, charged_by_key_id, memo, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tx_id,
            wallet_id,
            tx_type,
            amount_cents,
            related_tx_id,
            agent_id,
            normalized_key_id,
            memo,
            _now(),
        ),
    )
    return tx_id


def _resolve_charged_by_key_id(
    conn: sqlite3.Connection,
    charged_by_key_id: str | None,
    related_tx_id: str | None,
) -> str | None:
    normalized = str(charged_by_key_id or "").strip()
    if normalized:
        return normalized
    related = str(related_tx_id or "").strip()
    if not related:
        return None
    row = conn.execute(
        """
        SELECT charged_by_key_id
        FROM transactions
        WHERE tx_id = ?
        LIMIT 1
        """,
        (related,),
    ).fetchone()
    if row is None:
        return None
    inherited = str(row["charged_by_key_id"] or "").strip()
    return inherited or None


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
            "daily_spend_limit_cents": None,
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


def set_wallet_daily_spend_limit(wallet_id: str, daily_spend_limit_cents: int | None) -> dict:
    normalized_limit = None
    if daily_spend_limit_cents is not None:
        normalized_limit = int(daily_spend_limit_cents)
        if normalized_limit < 0:
            raise ValueError("daily_spend_limit_cents must be >= 0.")
    with _conn() as conn:
        updated = conn.execute(
            """
            UPDATE wallets
            SET daily_spend_limit_cents = ?
            WHERE wallet_id = ?
            """,
            (normalized_limit, wallet_id),
        ).rowcount
        if updated == 0:
            raise ValueError(f"Wallet '{wallet_id}' not found.")
        row = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
    return dict(row)


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

def pre_call_charge(
    caller_wallet_id: str,
    price_cents: int,
    agent_id: str,
    *,
    charged_by_key_id: str | None = None,
    max_spend_cents: int | None = None,
) -> str:
    """
    Transaction 1 (pre-call): check balance, deduct charge, update cache.
    Returns charge_tx_id. Raises InsufficientBalanceError if underfunded.
    DB lock held only for this short read-check-write sequence.
    """
    if price_cents < 0:
        raise ValueError("price_cents must be non-negative.")
    normalized_key_id = str(charged_by_key_id or "").strip() or None
    normalized_max_spend = None
    if max_spend_cents is not None:
        normalized_max_spend = int(max_spend_cents)
        if normalized_max_spend < 0:
            raise ValueError("max_spend_cents must be >= 0.")
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT balance_cents, daily_spend_limit_cents FROM wallets WHERE wallet_id = ?",
            (caller_wallet_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Wallet '{caller_wallet_id}' not found.")
        if normalized_key_id is not None and normalized_max_spend is not None:
            spent_row = conn.execute(
                """
                SELECT COALESCE(SUM(-amount_cents), 0) AS net_spent_cents
                FROM transactions
                WHERE wallet_id = ?
                  AND charged_by_key_id = ?
                  AND type IN ('charge', 'refund')
                """,
                (caller_wallet_id, normalized_key_id),
            ).fetchone()
            net_spent = int(spent_row["net_spent_cents"] or 0) if spent_row else 0
            if net_spent < 0:
                net_spent = 0
            if net_spent + price_cents > normalized_max_spend:
                raise KeySpendLimitExceededError(
                    limit_cents=normalized_max_spend,
                    spent_cents=net_spent,
                    attempted_cents=price_cents,
                )
        wallet_daily_limit_raw = row["daily_spend_limit_cents"]
        if wallet_daily_limit_raw is not None:
            daily_limit_cents = int(wallet_daily_limit_raw)
            now_dt = datetime.now(timezone.utc)
            since_iso = (now_dt - timedelta(hours=24)).isoformat()
            spent_daily_row = conn.execute(
                """
                SELECT COALESCE(SUM(-amount_cents), 0) AS net_spent_cents
                FROM transactions
                WHERE wallet_id = ?
                  AND type IN ('charge', 'refund')
                  AND created_at >= ?
                """,
                (caller_wallet_id, since_iso),
            ).fetchone()
            net_spent_daily = int(spent_daily_row["net_spent_cents"] or 0) if spent_daily_row else 0
            if net_spent_daily < 0:
                net_spent_daily = 0
            if net_spent_daily + price_cents > daily_limit_cents:
                raise WalletDailySpendLimitExceededError(
                    limit_cents=daily_limit_cents,
                    spent_last_24h_cents=net_spent_daily,
                    attempted_cents=price_cents,
                )
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
            normalized_key_id,
        )


def post_call_payout(
    agent_wallet_id: str,
    platform_wallet_id: str,
    charge_tx_id: str,
    price_cents: int,
    agent_id: str,
    *,
    platform_fee_pct: int | None = None,
    fee_bearer_policy: str | None = None,
) -> None:
    """
    Transaction 2a (success): credit 90% to agent, 10% to platform.
    Both inserts happen in one atomic transaction.
    Fee rounds down; agent gets the remainder to avoid creating or destroying cents.
    """
    distribution = compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct,
        fee_bearer_policy=fee_bearer_policy,
    )
    fee_cents = int(distribution["platform_fee_cents"])
    agent_cents = int(distribution["agent_payout_cents"])

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
            if agent_cents > 0:
                _insert_tx(
                    conn, agent_wallet_id, "payout", agent_cents, agent_id,
                    charge_tx_id, f"Agent payout for call {charge_tx_id[:8]}",
                )
                applied = True
        except sqlite3.IntegrityError:
            pass  # idempotency: payout already recorded
        try:
            if fee_cents > 0:
                _insert_tx(
                    conn, platform_wallet_id, "fee", fee_cents, agent_id,
                    charge_tx_id, f"Platform fee for call {charge_tx_id[:8]}",
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
            "fee_bearer_policy": normalize_fee_bearer_policy(fee_bearer_policy),
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
    *,
    platform_fee_pct: int | None = None,
    fee_bearer_policy: str | None = None,
    caller_charge_cents: int | None = None,
) -> None:
    """
    Partial settlement: refund a fraction of the charge to the caller and
    pay the remainder to the agent (90%) + platform (10%).

    Used when an agent fails after spending some compute — e.g., bad input
    validation that consumed tokens, or partial work before a downstream error.

    refund_fraction=1.0  →  full refund (identical to post_call_refund)
    refund_fraction=0.0  →  full payout to agent (identical to post_call_payout)
    """
    distribution = compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct,
        fee_bearer_policy=fee_bearer_policy,
    )
    total_charge_cents = int(
        distribution["caller_charge_cents"]
        if caller_charge_cents is None
        else max(0, int(caller_charge_cents))
    )
    total_success_cents = int(distribution["agent_payout_cents"] + distribution["platform_fee_cents"])
    refund_fraction = max(0.0, min(1.0, float(refund_fraction)))
    refund_cents = int(
        (Decimal(total_charge_cents) * Decimal(str(refund_fraction))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    kept_cents = max(0, total_charge_cents - refund_cents)
    if total_success_cents <= 0 or kept_cents <= 0:
        agent_cents = 0
        fee_cents = 0
    else:
        ratio = Decimal(distribution["platform_fee_cents"]) / Decimal(total_success_cents)
        fee_cents = int((Decimal(kept_cents) * ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        fee_cents = max(0, min(fee_cents, kept_cents))
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
            "caller_charge_cents": total_charge_cents,
            "refund_fraction": refund_fraction,
            "refund_cents": refund_cents,
            "agent_payout_cents": agent_cents,
            "platform_fee_cents": fee_cents,
            "fee_bearer_policy": normalize_fee_bearer_policy(fee_bearer_policy),
            "applied": applied,
        },
    )


