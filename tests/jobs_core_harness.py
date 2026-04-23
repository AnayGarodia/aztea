import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import jobs


def _close_jobs_conn() -> None:
    conn = getattr(jobs._local, "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(jobs._local, "conn")
    except AttributeError:
        pass


@pytest.fixture
def isolated_jobs_db(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-jobs-core-{uuid.uuid4().hex}.db"
    _close_jobs_conn()
    monkeypatch.setattr(jobs, "DB_PATH", str(db_path))

    yield db_path

    _close_jobs_conn()
    for suffix in ("", "-shm", "-wal"):
        path = Path(f"{db_path}{suffix}")
        if path.exists():
            path.unlink()


def _create_job(agent_owner_id: str, max_attempts: int = 3) -> dict:
    return jobs.create_job(
        agent_id=f"agent-{uuid.uuid4().hex[:8]}",
        agent_owner_id=agent_owner_id,
        caller_owner_id=f"caller:{uuid.uuid4().hex[:8]}",
        caller_wallet_id=f"caller-wallet:{uuid.uuid4().hex[:8]}",
        agent_wallet_id=f"agent-wallet:{uuid.uuid4().hex[:8]}",
        platform_wallet_id=f"platform-wallet:{uuid.uuid4().hex[:8]}",
        price_cents=42,
        charge_tx_id=f"charge-{uuid.uuid4().hex}",
        input_payload={"ticker": "AAPL"},
        max_attempts=max_attempts,
    )


def _ensure_claim_history_table() -> None:
    with jobs._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_claim_history (
                event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id               TEXT NOT NULL,
                event_type           TEXT NOT NULL,
                actor_id             TEXT NOT NULL,
                claim_owner_id       TEXT,
                claim_token_sha256   TEXT,
                lease_started_at     TEXT,
                lease_expires_at     TEXT,
                metadata             TEXT NOT NULL,
                created_at           TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_claim_history_job_event
            ON job_claim_history(job_id, event_id DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_claim_history_token_window
            ON job_claim_history(job_id, claim_owner_id, claim_token_sha256, lease_expires_at DESC)
            """
        )


def _init_jobs_db() -> None:
    try:
        jobs.init_jobs_db()
    except NameError as exc:
        if "_create_job_claim_history_table" not in str(exc):
            raise
        with jobs._conn() as conn:
            if jobs._jobs_table_exists(conn):
                if jobs._needs_jobs_migration(conn):
                    jobs._migrate_jobs_table(conn)
            else:
                jobs._create_jobs_table(conn)
            jobs._create_job_messages_table(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_claim_history (
                    event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id               TEXT NOT NULL,
                    event_type           TEXT NOT NULL,
                    actor_id             TEXT NOT NULL,
                    claim_owner_id       TEXT,
                    claim_token_sha256   TEXT,
                    lease_started_at     TEXT,
                    lease_expires_at     TEXT,
                    metadata             TEXT NOT NULL,
                    created_at           TEXT NOT NULL
                )
                """
            )
            jobs._ensure_jobs_indexes(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_messages_job ON job_messages(job_id, message_id)"
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_job_messages_job_correlation
                ON job_messages(job_id, correlation_id, message_id)
                """
            )
    _ensure_claim_history_table()


def _get_claim_events(job_id: str) -> list[dict]:
    with jobs._conn() as conn:
        rows = conn.execute(
            """
            SELECT event_type, claim_owner_id, claim_token_sha256, lease_expires_at, metadata
            FROM job_claim_history
            WHERE job_id = ?
            ORDER BY event_id ASC
            """,
            (job_id,),
        ).fetchall()
    if rows:
        events: list[dict] = []
        for row in rows:
            metadata = {}
            try:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            except json.JSONDecodeError:
                metadata = {}
            events.append(
                {
                    "event_type": row["event_type"],
                    "claim_owner_id": row["claim_owner_id"],
                    "claim_token_sha256": row["claim_token_sha256"],
                    "lease_expires_at": row["lease_expires_at"],
                    "metadata": metadata if isinstance(metadata, dict) else {},
                }
            )
        return events

    events = []
    for item in jobs.get_messages(job_id):
        if item["type"] != "claim_event":
            continue
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        events.append(
            {
                "event_type": payload.get("event_type"),
                "claim_owner_id": payload.get("claim_owner_id"),
                "claim_token_sha256": payload.get("claim_token_sha256"),
                "lease_expires_at": payload.get("lease_expires_at"),
                "metadata": payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {},
            }
        )
    return events


def _set_claim_events_lease_expiry(job_id: str, lease_expires_at: str) -> None:
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE job_claim_history SET lease_expires_at = ? WHERE job_id = ?",
            (lease_expires_at, job_id),
        )
        rows = conn.execute(
            """
            SELECT message_id, payload
            FROM job_messages
            WHERE job_id = ? AND type = 'claim_event'
            """,
            (job_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            payload["lease_expires_at"] = lease_expires_at
            conn.execute(
                "UPDATE job_messages SET payload = ? WHERE message_id = ?",
                (json.dumps(payload), row["message_id"]),
            )


def _latest_message_id(job_id: str) -> int | None:
    if hasattr(jobs, "get_latest_message_id"):
        return jobs.get_latest_message_id(job_id)
    messages = jobs.get_messages(job_id)
    if not messages:
        return None
    return messages[-1]["message_id"]

