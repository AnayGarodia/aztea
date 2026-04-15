import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from core import jobs
from core import models


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


def test_init_jobs_db_migrates_legacy_jobs_table(isolated_jobs_db):
    with sqlite3.connect(isolated_jobs_db) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
                job_id             TEXT PRIMARY KEY,
                agent_id           TEXT NOT NULL,
                caller_owner_id    TEXT NOT NULL,
                caller_wallet_id   TEXT NOT NULL,
                agent_wallet_id    TEXT NOT NULL,
                platform_wallet_id TEXT NOT NULL,
                status             TEXT NOT NULL,
                price_cents        INTEGER NOT NULL,
                charge_tx_id       TEXT NOT NULL,
                input_payload      TEXT NOT NULL,
                output_payload     TEXT,
                error_message      TEXT,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                completed_at       TEXT,
                settled_at         TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, agent_id, caller_owner_id, caller_wallet_id, agent_wallet_id,
                platform_wallet_id, status, price_cents, charge_tx_id, input_payload,
                output_payload, error_message, created_at, updated_at, completed_at, settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job-1",
                "legacy-agent",
                "caller:legacy",
                "caller-wallet",
                "agent-wallet",
                "platform-wallet",
                "pending",
                12,
                "legacy-charge",
                '{"ticker": "MSFT"}',
                None,
                None,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                None,
                None,
            ),
        )

    _init_jobs_db()

    with sqlite3.connect(isolated_jobs_db) as conn:
        conn.row_factory = sqlite3.Row
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        required = {
            "agent_owner_id",
            "claim_owner_id",
            "claim_token",
            "lease_expires_at",
            "last_heartbeat_at",
            "attempt_count",
            "max_attempts",
            "retry_count",
            "next_retry_at",
            "last_retry_at",
            "timeout_count",
            "last_timeout_at",
        }
        assert required.issubset(cols)

    migrated = jobs.get_job("legacy-job-1")
    assert migrated is not None
    assert migrated["agent_owner_id"] == "agent:legacy-agent"
    assert migrated["attempt_count"] == 0
    assert migrated["max_attempts"] == 3
    assert migrated["retry_count"] == 0
    assert migrated["timeout_count"] == 0


def test_init_jobs_db_migration_succeeds_with_foreign_key_dependents(isolated_jobs_db):
    with sqlite3.connect(isolated_jobs_db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            CREATE TABLE jobs (
                job_id             TEXT PRIMARY KEY,
                agent_id           TEXT NOT NULL,
                caller_owner_id    TEXT NOT NULL,
                caller_wallet_id   TEXT NOT NULL,
                agent_wallet_id    TEXT NOT NULL,
                platform_wallet_id TEXT NOT NULL,
                status             TEXT NOT NULL,
                price_cents        INTEGER NOT NULL,
                charge_tx_id       TEXT NOT NULL,
                input_payload      TEXT NOT NULL,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, agent_id, caller_owner_id, caller_wallet_id, agent_wallet_id,
                platform_wallet_id, status, price_cents, charge_tx_id, input_payload,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job-fk",
                "legacy-agent-fk",
                "caller:legacy",
                "caller-wallet",
                "agent-wallet",
                "platform-wallet",
                "pending",
                21,
                "legacy-charge-fk",
                '{"ticker": "AAPL"}',
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            CREATE TABLE disputes (
                dispute_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(job_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO disputes (dispute_id, job_id) VALUES (?, ?)",
            ("disp-1", "legacy-job-fk"),
        )

    _init_jobs_db()

    with sqlite3.connect(isolated_jobs_db) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        check_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert check_rows == []
        dispute = conn.execute(
            "SELECT dispute_id, job_id FROM disputes WHERE dispute_id = ?",
            ("disp-1",),
        ).fetchone()
        assert dispute is not None
        assert dispute["job_id"] == "legacy-job-fk"

    migrated = jobs.get_job("legacy-job-fk")
    assert migrated is not None
    assert migrated["agent_owner_id"] == "agent:legacy-agent-fk"


def test_claim_and_heartbeat_primitives_track_attempts(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:owner-1")

    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:owner-1", lease_seconds=60)
    assert claimed is not None
    assert claimed["status"] == "running"
    assert claimed["claim_owner_id"] == "worker:owner-1"
    assert claimed["attempt_count"] == 1

    first_token = claimed["claim_token"]
    first_lease_expiry = datetime.fromisoformat(claimed["lease_expires_at"])

    assert jobs.claim_job(job["job_id"], claim_owner_id="worker:owner-2", lease_seconds=60) is None

    reclaimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:owner-1", lease_seconds=120)
    assert reclaimed is not None
    assert reclaimed["attempt_count"] == 1
    assert reclaimed["claim_token"] == first_token
    assert datetime.fromisoformat(reclaimed["lease_expires_at"]) > first_lease_expiry

    heartbeat = jobs.heartbeat_job_lease(
        job["job_id"],
        claim_owner_id="worker:owner-1",
        claim_token=first_token,
        lease_seconds=180,
    )
    assert heartbeat is not None
    assert datetime.fromisoformat(heartbeat["lease_expires_at"]) > datetime.fromisoformat(
        reclaimed["lease_expires_at"]
    )

    unchanged = jobs.heartbeat_job_lease(
        job["job_id"],
        claim_owner_id="worker:owner-1",
        claim_token=first_token,
        lease_seconds=1,
    )
    assert unchanged is not None
    assert datetime.fromisoformat(unchanged["lease_expires_at"]) >= datetime.fromisoformat(
        heartbeat["lease_expires_at"]
    )

    assert jobs.heartbeat_job_lease(
        job["job_id"],
        claim_owner_id="worker:owner-1",
        claim_token="wrong-token",
        lease_seconds=60,
    ) is None


def test_retry_and_timeout_queries(isolated_jobs_db):
    _init_jobs_db()

    retry_job = _create_job(agent_owner_id="worker:retry")
    retry_claim = jobs.claim_job(retry_job["job_id"], claim_owner_id="worker:retry", lease_seconds=30)
    assert retry_claim is not None

    scheduled = jobs.schedule_job_retry(
        retry_job["job_id"],
        retry_delay_seconds=0,
        error_message="transient failure",
        claim_owner_id="worker:retry",
        claim_token=retry_claim["claim_token"],
    )
    assert scheduled is not None
    assert scheduled["status"] == "pending"
    assert scheduled["retry_count"] == 1

    due_retry_ids = {item["job_id"] for item in jobs.list_jobs_due_for_retry()}
    assert retry_job["job_id"] in due_retry_ids

    no_retry_job = _create_job(agent_owner_id="worker:no-retry", max_attempts=1)
    no_retry_claim = jobs.claim_job(no_retry_job["job_id"], claim_owner_id="worker:no-retry", lease_seconds=30)
    assert no_retry_claim is not None

    exhausted = jobs.schedule_job_retry(
        no_retry_job["job_id"],
        retry_delay_seconds=0,
        error_message="max attempts reached",
        claim_owner_id="worker:no-retry",
        claim_token=no_retry_claim["claim_token"],
    )
    assert exhausted is not None
    assert exhausted["status"] == "failed"
    assert exhausted["next_retry_at"] is None

    due_retry_ids = {item["job_id"] for item in jobs.list_jobs_due_for_retry()}
    assert no_retry_job["job_id"] not in due_retry_ids

    timeout_job = _create_job(agent_owner_id="worker:timeout")
    timeout_claim = jobs.claim_job(timeout_job["job_id"], claim_owner_id="worker:timeout", lease_seconds=60)
    assert timeout_claim is not None

    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ?, status = 'running' WHERE job_id = ?",
            (expired_at, timeout_job["job_id"]),
        )

    expired_ids = {item["job_id"] for item in jobs.list_jobs_with_expired_leases()}
    assert timeout_job["job_id"] in expired_ids

    timed_out = jobs.mark_job_timeout(timeout_job["job_id"], retry_delay_seconds=0)
    assert timed_out is not None
    assert timed_out["timeout_count"] == 1
    assert timed_out["status"] == "pending"

    due_retry_ids = {item["job_id"] for item in jobs.list_jobs_due_for_retry()}
    assert timeout_job["job_id"] in due_retry_ids


def test_authorization_helpers_expose_owner_context(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:owner-ctx")

    context = jobs.get_job_authorization_context(job["job_id"])
    assert context is not None
    assert context["agent_id"] == job["agent_id"]
    assert context["agent_owner_id"] == "worker:owner-ctx"
    assert context["caller_owner_id"] == job["caller_owner_id"]
    assert context["claim_owner_id"] is None

    assert jobs.is_worker_authorized(job, "worker:owner-ctx")
    assert not jobs.is_worker_authorized(job, "worker:someone-else")
    assert jobs.is_worker_authorized_for_job(job["job_id"], "worker:owner-ctx")
    assert not jobs.is_worker_authorized_for_job(job["job_id"], "worker:someone-else")

    assert jobs.claim_job(job["job_id"], claim_owner_id="worker:someone-else") is None


def test_list_jobs_for_owner_supports_cursor_pagination(isolated_jobs_db):
    _init_jobs_db()
    owner_id = "caller:page-owner"
    created: list[dict] = []

    for idx in range(5):
        job = jobs.create_job(
            agent_id=f"agent-page-{idx}",
            agent_owner_id=f"worker:page-{idx}",
            caller_owner_id=owner_id,
            caller_wallet_id=f"caller-wallet-page-{idx}",
            agent_wallet_id=f"agent-wallet-page-{idx}",
            platform_wallet_id=f"platform-wallet-page-{idx}",
            price_cents=10 + idx,
            charge_tx_id=f"charge-page-{idx}",
            input_payload={"n": idx},
            max_attempts=3,
        )
        ts = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=idx)).isoformat()
        with jobs._conn() as conn:
            conn.execute(
                "UPDATE jobs SET created_at = ?, updated_at = ? WHERE job_id = ?",
                (ts, ts, job["job_id"]),
            )
        created.append(jobs.get_job(job["job_id"]))

    page1 = jobs.list_jobs_for_owner(owner_id, limit=2)
    assert len(page1) == 2
    page1_ids = [item["job_id"] for item in page1]

    last = page1[-1]
    page2 = jobs.list_jobs_for_owner(
        owner_id,
        limit=2,
        before_created_at=last["created_at"],
        before_job_id=last["job_id"],
    )
    assert len(page2) == 2
    page2_ids = [item["job_id"] for item in page2]
    assert set(page1_ids).isdisjoint(page2_ids)

    page3 = jobs.list_jobs_for_owner(
        owner_id,
        limit=2,
        before_created_at=page2[-1]["created_at"],
        before_job_id=page2[-1]["job_id"],
    )
    assert len(page3) == 1
    assert page3[0]["job_id"] not in set(page1_ids + page2_ids)


def test_expired_lease_reclaim_rotates_claim_audit_fields(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:audit")

    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:audit", lease_seconds=60)
    assert claimed is not None

    first_token = claimed["claim_token"]
    old_claimed_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET lease_expires_at = ?, claimed_at = ?, last_heartbeat_at = ?, status = 'running'
            WHERE job_id = ?
            """,
            (expired_at, old_claimed_at, expired_at, job["job_id"]),
        )

    stale = jobs.get_job(job["job_id"])
    assert stale is not None
    assert jobs._lease_is_expired(stale, datetime.now(timezone.utc))

    reclaimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:audit", lease_seconds=120)
    assert reclaimed is not None
    assert reclaimed["attempt_count"] == 2
    assert reclaimed["claim_token"] != first_token
    assert datetime.fromisoformat(reclaimed["claimed_at"]) > datetime.fromisoformat(old_claimed_at)

    assert jobs.heartbeat_job_lease(
        job["job_id"],
        claim_owner_id="worker:audit",
        claim_token=first_token,
        lease_seconds=60,
    ) is None

    renewed = jobs.heartbeat_job_lease(
        job["job_id"],
        claim_owner_id="worker:audit",
        claim_token=reclaimed["claim_token"],
        lease_seconds=300,
    )
    assert renewed is not None
    assert datetime.fromisoformat(renewed["lease_expires_at"]) > datetime.fromisoformat(
        reclaimed["lease_expires_at"]
    )

    claim_events = _get_claim_events(job["job_id"])
    event_types = [event.get("event_type") for event in claim_events]
    assert "claim_acquired" in event_types
    assert "claim_reclaimed" in event_types
    assert "claim_heartbeat" in event_types
    assert all(len((event.get("claim_token_sha256") or "")) == 64 for event in claim_events)


def test_clarification_message_flow_preserves_claim_and_extends_lease(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:clarify")

    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:clarify", lease_seconds=60)
    assert claimed is not None
    claim_token = claimed["claim_token"]
    first_lease_expiry = datetime.fromisoformat(claimed["lease_expires_at"])

    asked = jobs.add_message(
        job["job_id"],
        from_id="worker:clarify",
        msg_type="clarification_needed",
        payload={"question": "Need additional context."},
        lease_seconds=120,
    )
    assert asked is not None

    awaiting = jobs.get_job(job["job_id"])
    assert awaiting is not None
    assert awaiting["status"] == "awaiting_clarification"
    assert awaiting["claim_owner_id"] == "worker:clarify"
    assert awaiting["claim_token"] == claim_token
    assert datetime.fromisoformat(awaiting["lease_expires_at"]) > first_lease_expiry

    first_extension_expiry = datetime.fromisoformat(awaiting["lease_expires_at"])

    answered = jobs.add_message(
        job["job_id"],
        from_id=job["caller_owner_id"],
        msg_type="clarification",
        payload={"answer": "Use fiscal-year totals."},
        lease_seconds=90,
    )
    assert answered is not None
    assert answered["message_id"] > asked["message_id"]

    resumed = jobs.get_job(job["job_id"])
    assert resumed is not None
    assert resumed["status"] == "running"
    assert resumed["claim_owner_id"] == "worker:clarify"
    assert resumed["claim_token"] == claim_token
    assert datetime.fromisoformat(resumed["lease_expires_at"]) > first_extension_expiry

    all_messages = jobs.get_messages(job["job_id"])
    human_messages = [item for item in all_messages if item["type"] != "claim_event"]
    claim_events = _get_claim_events(job["job_id"])

    assert [item["type"] for item in human_messages] == ["clarification_needed", "clarification"]
    assert _latest_message_id(job["job_id"]) == all_messages[-1]["message_id"]
    assert [item["message_id"] for item in jobs.get_messages(job["job_id"], since_id=asked["message_id"])] == [
        item["message_id"] for item in all_messages if item["message_id"] > asked["message_id"]
    ]

    event_types = [event.get("event_type") for event in claim_events]
    assert "claim_acquired" in event_types
    assert event_types.count("claim_lease_extended") >= 2


def test_claim_token_recent_activity_helper_respects_grace_window(isolated_jobs_db):
    _init_jobs_db()
    if not hasattr(jobs, "claim_token_was_recently_active"):
        pytest.skip("claim_token_was_recently_active helper is not available in this core build.")
    job = _create_job(agent_owner_id="worker:grace")

    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:grace", lease_seconds=45)
    assert claimed is not None
    claim_token = claimed["claim_token"]
    claim_owner_id = claimed["claim_owner_id"]

    assert jobs.claim_token_was_recently_active(
        job["job_id"],
        claim_owner_id=claim_owner_id,
        claim_token=claim_token,
        within_seconds=60,
    )

    stale_lease_expires_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _set_claim_events_lease_expiry(job["job_id"], stale_lease_expires_at)

    assert not jobs.claim_token_was_recently_active(
        job["job_id"],
        claim_owner_id=claim_owner_id,
        claim_token=claim_token,
        within_seconds=60,
    )

    refreshed = jobs.heartbeat_job_lease(
        job["job_id"],
        claim_owner_id=claim_owner_id,
        claim_token=claim_token,
        lease_seconds=120,
    )
    assert refreshed is not None

    assert jobs.claim_token_was_recently_active(
        job["job_id"],
        claim_owner_id=claim_owner_id,
        claim_token=claim_token,
        within_seconds=60,
    )


def test_lease_helpers_classify_stale_and_unclaimed_jobs(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:helpers")

    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:helpers", lease_seconds=60)
    assert claimed is not None

    now_dt = datetime.now(timezone.utc)
    assert jobs._lease_is_active(claimed, now_dt)
    assert not jobs._lease_is_expired(claimed, now_dt)

    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = NULL, status = 'running' WHERE job_id = ?",
            (job["job_id"],),
        )

    stale = jobs.get_job(job["job_id"])
    assert stale is not None
    assert not jobs._lease_is_active(stale, now_dt)
    assert jobs._lease_is_expired(stale, now_dt)
    assert job["job_id"] in {
        item["job_id"]
        for item in jobs.list_jobs_with_expired_leases(
            now=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        )
    }

    released = jobs.release_job_claim(
        job["job_id"],
        claim_owner_id="worker:helpers",
        claim_token=claimed["claim_token"],
    )
    assert released is not None
    assert not jobs._lease_is_active(released, now_dt)
    assert not jobs._lease_is_expired(released, now_dt)
    assert job["job_id"] not in {item["job_id"] for item in jobs.list_jobs_with_expired_leases()}


def test_terminal_status_updates_are_idempotent_after_completion(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:idempotent")

    first = jobs.update_job_status(
        job["job_id"],
        status="complete",
        output_payload={"ok": True},
        completed=True,
    )
    assert first is not None
    assert first["status"] == "complete"
    assert first["output_payload"] == {"ok": True}
    assert first["completed_at"] is not None

    second = jobs.update_job_status(
        job["job_id"],
        status="failed",
        error_message="late worker write should be ignored",
        completed=True,
    )
    assert second is not None
    assert second["status"] == "complete"
    assert second["output_payload"] == {"ok": True}
    assert second["error_message"] is None
    assert second["completed_at"] == first["completed_at"]


@pytest.mark.parametrize(
    ("body", "expected_type", "expected_correlation"),
    [
        (
            {
                "type": "clarification_request",
                "payload": {"question": " Need context ", "schema": {"fields": ["ticker"]}},
            },
            "clarification_request",
            None,
        ),
        (
            {
                "type": "clarification_response",
                "payload": {"answer": " Use GAAP totals ", "request_message_id": 7},
            },
            "clarification_response",
            None,
        ),
        (
            {
                "type": "progress",
                "payload": {"percent": 55, "note": " halfway "},
            },
            "progress",
            None,
        ),
        (
            {
                "type": "partial_result",
                "payload": {"payload": {"rows": 2}, "is_final": False},
            },
            "partial_result",
            None,
        ),
        (
            {
                "type": "artifact",
                "payload": {
                    "name": "brief.json",
                    "mime": "application/json",
                    "url_or_base64": "https://example.test/brief.json",
                    "size_bytes": 12,
                },
            },
            "artifact",
            None,
        ),
        (
            {
                "type": "tool_call",
                "payload": {
                    "tool_name": "search",
                    "args": {"ticker": "AAPL"},
                    "correlation_id": "corr-tool-call",
                },
            },
            "tool_call",
            "corr-tool-call",
        ),
        (
            {
                "type": "tool_result",
                "correlation_id": "corr-tool-result",
                "payload": {
                    "correlation_id": "corr-tool-result",
                    "payload": {"ok": True},
                    "error": " ",
                },
            },
            "tool_result",
            "corr-tool-result",
        ),
        (
            {
                "type": "note",
                "payload": {"text": "  worker note  "},
            },
            "note",
            None,
        ),
    ],
)
def test_parse_typed_job_message_accepts_all_supported_types(
    body: dict,
    expected_type: str,
    expected_correlation: str | None,
):
    parsed = models.parse_typed_job_message(body)
    normalized = parsed.model_dump()
    assert normalized["type"] == expected_type
    assert normalized.get("correlation_id") == expected_correlation


@pytest.mark.parametrize(
    "body",
    [
        {"type": "clarification_request", "payload": {"question": " "}},
        {"type": "clarification_response", "payload": {"answer": "ok"}},
        {"type": "progress", "payload": {"percent": 101}},
        {"type": "partial_result", "payload": {"payload": {}, "is_final": True}},
        {
            "type": "artifact",
            "payload": {"name": "n", "mime": "m", "url_or_base64": "u", "size_bytes": -1},
        },
        {"type": "tool_call", "payload": {"tool_name": " ", "args": {}}},
        {"type": "tool_result", "payload": {"payload": {"ok": True}}},
        {"type": "note", "payload": {"text": " "}},
    ],
)
def test_parse_typed_job_message_rejects_invalid_payloads(body: dict):
    with pytest.raises(ValidationError):
        models.parse_typed_job_message(body)


def test_normalize_job_message_body_supports_legacy_clarification_types():
    asked = models.normalize_job_message_body(
        msg_type="clarification_needed",
        payload={"question": "Need totals by segment."},
        allow_legacy=True,
    )
    assert asked["type"] == "clarification_needed"
    assert asked["canonical_type"] == "clarification_request"

    answered = models.normalize_job_message_body(
        msg_type="clarification",
        payload={"answer": "Use fiscal-year totals."},
        allow_legacy=True,
    )
    assert answered["type"] == "clarification"
    assert answered["canonical_type"] == "clarification_response"
    assert answered["payload"]["answer"] == "Use fiscal-year totals."


def test_clarification_request_message_marks_awaiting_and_extends_lease(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:typed-clarification-request")
    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:typed-clarification-request", lease_seconds=45)
    assert claimed is not None
    previous_expiry = datetime.fromisoformat(claimed["lease_expires_at"])

    message = jobs.add_message(
        job["job_id"],
        from_id="worker:typed-clarification-request",
        msg_type="clarification_request",
        payload={"question": "Need calendarized revenue and schema.", "schema": {"required": ["answer"]}},
        lease_seconds=120,
    )
    assert message is not None
    assert message["type"] == "clarification_request"

    updated = jobs.get_job(job["job_id"])
    assert updated is not None
    assert updated["status"] == "awaiting_clarification"
    assert datetime.fromisoformat(updated["lease_expires_at"]) > previous_expiry


def test_clarification_response_message_resumes_running_and_extends_lease(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:typed-clarification-response")
    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:typed-clarification-response", lease_seconds=45)
    assert claimed is not None

    asked = jobs.add_message(
        job["job_id"],
        from_id="worker:typed-clarification-response",
        msg_type="clarification_request",
        payload={"question": "Please provide region split."},
        lease_seconds=60,
    )
    awaiting = jobs.get_job(job["job_id"])
    assert awaiting is not None
    assert awaiting["status"] == "awaiting_clarification"
    previous_expiry = datetime.fromisoformat(awaiting["lease_expires_at"])

    message = jobs.add_message(
        job["job_id"],
        from_id=job["caller_owner_id"],
        msg_type="clarification_response",
        payload={"answer": {"region": "NA"}, "request_message_id": asked["message_id"]},
        lease_seconds=90,
    )
    assert message is not None
    assert message["type"] == "clarification_response"
    assert message["payload"]["request_message_id"] == asked["message_id"]

    resumed = jobs.get_job(job["job_id"])
    assert resumed is not None
    assert resumed["status"] == "running"
    assert datetime.fromisoformat(resumed["lease_expires_at"]) > previous_expiry


@pytest.mark.parametrize(
    ("msg_type", "payload", "correlation_id", "seed_tool_call"),
    [
        ("progress", {"percent": 25, "note": "working"}, None, False),
        ("partial_result", {"payload": {"summary": "part"}, "is_final": False}, None, False),
        (
            "artifact",
            {
                "name": "memo.txt",
                "mime": "text/plain",
                "url_or_base64": "VGhpcyBpcyBhIG1lbW8=",
                "size_bytes": 16,
            },
            None,
            False,
        ),
        (
            "tool_call",
            {"tool_name": "sec_lookup", "args": {"ticker": "AAPL"}, "correlation_id": "corr-typed-tool"},
            "corr-typed-tool",
            False,
        ),
        (
            "tool_result",
            {"correlation_id": "corr-typed-tool", "payload": {"status": "ok"}, "error": None},
            "corr-typed-tool",
            True,
        ),
        ("note", {"text": "worker checkpoint"}, None, False),
    ],
)
def test_extend_only_message_types_extend_lease_without_status_transition(
    isolated_jobs_db,
    msg_type: str,
    payload: dict,
    correlation_id: str | None,
    seed_tool_call: bool,
):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:typed-extend")
    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:typed-extend", lease_seconds=45)
    assert claimed is not None

    if seed_tool_call:
        jobs.add_message(
            job["job_id"],
            from_id="worker:typed-extend",
            msg_type="tool_call",
            payload={"tool_name": "sec_lookup", "args": {"ticker": "AAPL"}, "correlation_id": "corr-typed-tool"},
            lease_seconds=30,
        )

    baseline = jobs.get_job(job["job_id"])
    assert baseline is not None
    baseline_expiry = datetime.fromisoformat(baseline["lease_expires_at"])

    message = jobs.add_message(
        job["job_id"],
        from_id="worker:typed-extend",
        msg_type=msg_type,
        payload=payload,
        lease_seconds=75,
        correlation_id=correlation_id,
    )
    assert message is not None
    assert message["type"] == msg_type

    updated = jobs.get_job(job["job_id"])
    assert updated is not None
    assert updated["status"] == "running"
    assert datetime.fromisoformat(updated["lease_expires_at"]) > baseline_expiry


def test_tool_call_correlation_helpers_and_tool_result_reference_checks(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:corr")
    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:corr", lease_seconds=45)
    assert claimed is not None

    assert not jobs.tool_call_correlation_exists(job["job_id"], "corr-missing")
    assert not jobs.message_correlation_exists(job["job_id"], "corr-missing")

    with pytest.raises(ValueError, match="no matching tool_call"):
        jobs.add_message(
            job["job_id"],
            from_id="worker:corr",
            msg_type="tool_result",
            payload={"correlation_id": "corr-1", "payload": {"ok": False}, "error": None},
            lease_seconds=45,
        )

    tool_call = jobs.add_message(
        job["job_id"],
        from_id="worker:corr",
        msg_type="tool_call",
        payload={"tool_name": "sec_lookup", "args": {"ticker": "MSFT"}, "correlation_id": "corr-1"},
        lease_seconds=45,
    )
    assert tool_call["correlation_id"] == "corr-1"
    assert jobs.tool_call_correlation_exists(job["job_id"], "corr-1")
    assert jobs.message_correlation_exists(job["job_id"], "corr-1")
    assert jobs.message_correlation_exists(job["job_id"], "corr-1", msg_type="tool_call")

    tool_result = jobs.add_message(
        job["job_id"],
        from_id="worker:corr",
        msg_type="tool_result",
        payload={"correlation_id": "corr-1", "payload": {"ok": True}, "error": None},
        lease_seconds=45,
    )
    assert tool_result["correlation_id"] == "corr-1"
    assert jobs.message_correlation_exists(job["job_id"], "corr-1", msg_type="tool_result")


def test_init_jobs_db_migrates_job_messages_for_correlation_id(isolated_jobs_db):
    with sqlite3.connect(isolated_jobs_db) as conn:
        jobs._create_jobs_table(conn)
        conn.execute(
            """
            CREATE TABLE job_messages (
                message_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id       TEXT NOT NULL,
                from_id      TEXT NOT NULL,
                type         TEXT NOT NULL,
                payload      TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
            """
        )

    _init_jobs_db()

    with sqlite3.connect(isolated_jobs_db) as conn:
        conn.row_factory = sqlite3.Row
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(job_messages)").fetchall()}
        assert "correlation_id" in cols
        indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(job_messages)").fetchall()
        }
        assert "idx_job_messages_job_correlation" in indexes
