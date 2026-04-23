"""Job DB core tests (schema, claims, leases)."""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from core import jobs

from tests.jobs_core_harness import (
    isolated_jobs_db,
    _create_job,
    _get_claim_events,
    _init_jobs_db,
    _latest_message_id,
    _set_claim_events_lease_expiry,
)

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
            "tree_depth",
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

