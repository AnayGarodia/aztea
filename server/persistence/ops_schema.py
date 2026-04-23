"""Job-event / idempotency / Stripe bookkeeping tables on the jobs SQLite DB."""

from __future__ import annotations

import sqlite3

from core.db import get_db_connection
from core import jobs


def migrate_job_event_deliveries_status_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job_event_deliveries'"
    ).fetchone()
    if row is None:
        return
    table_sql = str(row["sql"] or "").lower()
    if (
        "dead_letter" not in table_sql
        and "retrying" not in table_sql
        and "'failed'" in table_sql
        and "'cancelled'" in table_sql
    ):
        return

    conn.execute(
        """
        CREATE TABLE job_event_deliveries_new (
            delivery_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id            INTEGER NOT NULL,
            hook_id             TEXT NOT NULL,
            owner_id            TEXT NOT NULL,
            target_url          TEXT NOT NULL,
            secret              TEXT,
            payload             TEXT NOT NULL,
            status              TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'failed', 'cancelled')),
            attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            next_attempt_at     TEXT NOT NULL,
            last_attempt_at     TEXT,
            last_success_at     TEXT,
            last_status_code    INTEGER,
            last_error          TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            UNIQUE(event_id, hook_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO job_event_deliveries_new (
            delivery_id, event_id, hook_id, owner_id, target_url, secret, payload, status,
            attempt_count, next_attempt_at, last_attempt_at, last_success_at, last_status_code,
            last_error, created_at, updated_at
        )
        SELECT
            delivery_id,
            event_id,
            hook_id,
            owner_id,
            target_url,
            secret,
            payload,
            CASE
                WHEN status = 'retrying' THEN 'pending'
                WHEN status = 'dead_letter' THEN 'failed'
                WHEN status IN ('pending', 'delivered', 'failed', 'cancelled') THEN status
                ELSE 'pending'
            END AS status,
            attempt_count,
            next_attempt_at,
            last_attempt_at,
            last_success_at,
            last_status_code,
            last_error,
            created_at,
            updated_at
        FROM job_event_deliveries
        """
    )
    conn.execute("DROP TABLE job_event_deliveries")
    conn.execute("ALTER TABLE job_event_deliveries_new RENAME TO job_event_deliveries")


def init_ops_db() -> None:
    with jobs._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id            TEXT NOT NULL,
                agent_id          TEXT NOT NULL,
                agent_owner_id    TEXT NOT NULL,
                caller_owner_id   TEXT NOT NULL,
                event_type        TEXT NOT NULL,
                actor_owner_id    TEXT,
                payload           TEXT NOT NULL DEFAULT '{}',
                created_at        TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_event_hooks (
                hook_id            TEXT PRIMARY KEY,
                owner_id           TEXT NOT NULL,
                target_url         TEXT NOT NULL,
                secret             TEXT,
                is_active          INTEGER NOT NULL DEFAULT 1,
                created_at         TEXT NOT NULL,
                last_attempt_at    TEXT,
                last_success_at    TEXT,
                last_status_code   INTEGER,
                last_error         TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_event_deliveries (
                delivery_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id            INTEGER NOT NULL,
                hook_id             TEXT NOT NULL,
                owner_id            TEXT NOT NULL,
                target_url          TEXT NOT NULL,
                secret              TEXT,
                payload             TEXT NOT NULL,
                status              TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'failed', 'cancelled')),
                attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                next_attempt_at     TEXT NOT NULL,
                last_attempt_at     TEXT,
                last_success_at     TEXT,
                last_status_code    INTEGER,
                last_error          TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                UNIQUE(event_id, hook_id)
            )
            """
        )
        migrate_job_event_deliveries_status_schema(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_owner_created ON job_events(caller_owner_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_agent_owner_created ON job_events(agent_owner_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_job_created ON job_events(job_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_hooks_owner_active ON job_event_hooks(owner_id, is_active)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_event_deliveries_status_due
            ON job_event_deliveries(status, next_attempt_at, delivery_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_event_deliveries_owner_created
            ON job_event_deliveries(owner_id, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_requests (
                request_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id         TEXT NOT NULL,
                scope            TEXT NOT NULL,
                idempotency_key  TEXT NOT NULL,
                request_hash     TEXT NOT NULL,
                status           TEXT NOT NULL CHECK(status IN ('in_progress', 'completed')),
                response_status  INTEGER,
                response_body    TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                UNIQUE(owner_id, scope, idempotency_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_updated ON idempotency_requests(updated_at DESC)"
        )


def init_stripe_db() -> None:
    """Create Stripe bookkeeping tables used for top-ups and webhook idempotency."""
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_sessions (
                session_id    TEXT PRIMARY KEY,
                wallet_id     TEXT NOT NULL,
                amount_cents  INTEGER NOT NULL,
                processed_at  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_webhook_events (
                session_id    TEXT PRIMARY KEY,
                wallet_id     TEXT NOT NULL,
                amount_cents  INTEGER NOT NULL,
                status        TEXT NOT NULL CHECK(status IN ('processing', 'processed', 'failed')),
                attempts      INTEGER NOT NULL DEFAULT 0,
                last_error    TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stripe_webhook_events_status_updated "
            "ON stripe_webhook_events(status, updated_at DESC)"
        )
