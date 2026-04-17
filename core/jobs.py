"""
jobs.py — Async job system for agentmarket.

Jobs are created by callers and settled by agents. Each job is charged up front,
then paid out (or refunded) on completion. Messages attach to jobs to allow
clarifications without holding open HTTP connections.
"""

import json
import hashlib
import os
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from core import models as _models
from core import db as _db

DB_PATH = _db.DB_PATH
_local = _db._local

_CANONICAL_CREATED_AT = "1970-01-01T00:00:00+00:00"
DEFAULT_LEASE_SECONDS = 300
_CLAIM_EVENT_MSG_TYPE = "claim_event"
_CLAIM_EVENT_ACTOR = "system:jobs"
_ACTIVE_CLAIM_EVENT_TYPES = {
    "claim_acquired",
    "claim_reclaimed",
    "claim_heartbeat",
    "claim_lease_extended",
}
_LEASE_BEHAVIOR_EXTEND = "extend"
_LEASE_BEHAVIOR_EXTEND_AND_MARK_AWAITING = "extend_and_mark_awaiting"
_LEASE_BEHAVIOR_EXTEND_AND_RESUME_RUNNING = "extend_and_resume_running"
MESSAGE_TYPE_LEASE_BEHAVIOR = {
    "clarification_request": _LEASE_BEHAVIOR_EXTEND_AND_MARK_AWAITING,
    "clarification_response": _LEASE_BEHAVIOR_EXTEND_AND_RESUME_RUNNING,
    "progress": _LEASE_BEHAVIOR_EXTEND,
    "partial_result": _LEASE_BEHAVIOR_EXTEND,
    "artifact": _LEASE_BEHAVIOR_EXTEND,
    "tool_call": _LEASE_BEHAVIOR_EXTEND,
    "tool_result": _LEASE_BEHAVIOR_EXTEND,
    "note": _LEASE_BEHAVIOR_EXTEND,
}
_LEGACY_MESSAGE_TYPE_LEASE_BEHAVIOR = {
    "clarification_needed": _LEASE_BEHAVIOR_EXTEND_AND_MARK_AWAITING,
    "clarification": _LEASE_BEHAVIOR_EXTEND_AND_RESUME_RUNNING,
}
_JOB_MESSAGE_SUBSCRIBERS_LOCK = threading.Lock()
_JOB_MESSAGE_SUBSCRIBERS: dict[str, set[queue.Queue]] = {}

VALID_STATUSES = {
    "pending",
    "running",
    "awaiting_clarification",
    "complete",
    "failed",
}

_CLAIMABLE_STATUSES = {
    "pending",
    "running",
    "awaiting_clarification",
}

_ACTIVE_LEASE_STATUSES = {
    "running",
    "awaiting_clarification",
}

_CANONICAL_JOB_COLUMNS = (
    "job_id",
    "agent_id",
    "agent_owner_id",
    "caller_owner_id",
    "caller_wallet_id",
    "agent_wallet_id",
    "platform_wallet_id",
    "status",
    "price_cents",
    "charge_tx_id",
    "input_payload",
    "output_payload",
    "error_message",
    "created_at",
    "updated_at",
    "completed_at",
    "settled_at",
    "claim_owner_id",
    "claim_token",
    "claimed_at",
    "lease_expires_at",
    "last_heartbeat_at",
    "attempt_count",
    "max_attempts",
    "retry_count",
    "next_retry_at",
    "last_retry_at",
    "timeout_count",
    "last_timeout_at",
    "dispute_window_hours",
    "dispute_outcome",
    "judge_agent_id",
    "judge_verdict",
    "quality_score",
    "callback_url",
    "callback_secret",
    "batch_id",
)

_REQUIRED_JOB_COLUMNS = set(_CANONICAL_JOB_COLUMNS)


def _conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL mode."""
    return _db.get_raw_connection(DB_PATH)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _iso_after_seconds(seconds: int) -> str:
    return (_now_dt() + timedelta(seconds=seconds)).isoformat()


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_non_negative_int(value, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _clean_optional_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_required_json(value, default) -> str:
    if value is None:
        return json.dumps(default)
    if isinstance(value, (dict, list, str, int, float, bool)):
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return json.dumps(default)
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return json.dumps(default)
            return json.dumps(parsed)
        return json.dumps(value)
    return json.dumps(default)


def _normalize_optional_json(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return json.dumps(parsed)
    if isinstance(value, (dict, list, int, float, bool)):
        return json.dumps(value)
    return None


def _claim_token_sha256(token: str | None) -> str | None:
    cleaned = _clean_optional_text(token)
    if cleaned is None:
        return None
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _insert_job_message_row(
    conn: sqlite3.Connection,
    job_id: str,
    from_id: str,
    msg_type: str,
    payload: dict,
    correlation_id: str | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO job_messages (job_id, from_id, type, payload, correlation_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job_id, from_id, msg_type, json.dumps(payload), correlation_id, created_at),
    )
    return int(cur.lastrowid)


def _insert_claim_event_row(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    event_type: str,
    claim_owner_id: str | None,
    claim_token: str | None,
    lease_started_at: str | None,
    lease_expires_at: str | None,
    actor_id: str | None = None,
    metadata: dict | None = None,
    created_at: str | None = None,
) -> int:
    payload: dict = {
        "event_type": event_type,
        "claim_owner_id": _clean_optional_text(claim_owner_id),
        "claim_token_sha256": _claim_token_sha256(claim_token),
        "lease_started_at": _clean_optional_text(lease_started_at),
        "lease_expires_at": _clean_optional_text(lease_expires_at),
    }
    if metadata:
        payload["metadata"] = metadata
    return _insert_job_message_row(
        conn,
        job_id=job_id,
        from_id=(actor_id or _CLAIM_EVENT_ACTOR),
        msg_type=_CLAIM_EVENT_MSG_TYPE,
        payload=payload,
        correlation_id=None,
        created_at=created_at or _now(),
    )


def _publish_job_message(job_id: str, message: dict | None) -> None:
    if message is None:
        return
    with _JOB_MESSAGE_SUBSCRIBERS_LOCK:
        subscribers = list(_JOB_MESSAGE_SUBSCRIBERS.get(job_id, set()))
    for subscriber in subscribers:
        subscriber.put_nowait(message)


def subscribe_job_messages(job_id: str) -> queue.Queue:
    subscriber: queue.Queue = queue.Queue()
    with _JOB_MESSAGE_SUBSCRIBERS_LOCK:
        _JOB_MESSAGE_SUBSCRIBERS.setdefault(job_id, set()).add(subscriber)
    return subscriber


def unsubscribe_job_messages(job_id: str, subscriber: queue.Queue) -> None:
    with _JOB_MESSAGE_SUBSCRIBERS_LOCK:
        subscribers = _JOB_MESSAGE_SUBSCRIBERS.get(job_id)
        if subscribers is None:
            return
        subscribers.discard(subscriber)
        if not subscribers:
            _JOB_MESSAGE_SUBSCRIBERS.pop(job_id, None)


def _jobs_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()
    return row is not None


def _jobs_columns(conn: sqlite3.Connection) -> dict:
    return {
        row["name"]: row
        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }


def _create_jobs_table(conn: sqlite3.Connection, table_name: str = "jobs") -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            job_id              TEXT PRIMARY KEY,
            agent_id            TEXT NOT NULL,
            agent_owner_id      TEXT NOT NULL,
            caller_owner_id     TEXT NOT NULL,
            caller_wallet_id    TEXT NOT NULL,
            agent_wallet_id     TEXT NOT NULL,
            platform_wallet_id  TEXT NOT NULL,
            status              TEXT NOT NULL,
            price_cents         INTEGER NOT NULL CHECK(price_cents >= 0),
            charge_tx_id        TEXT NOT NULL,
            input_payload       TEXT NOT NULL,
            output_payload      TEXT,
            error_message       TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            completed_at        TEXT,
            settled_at          TEXT,
            claim_owner_id      TEXT,
            claim_token         TEXT,
            claimed_at          TEXT,
            lease_expires_at    TEXT,
            last_heartbeat_at   TEXT,
            attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            max_attempts        INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts >= 1),
            retry_count         INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
            next_retry_at       TEXT,
            last_retry_at       TEXT,
            timeout_count       INTEGER NOT NULL DEFAULT 0 CHECK(timeout_count >= 0),
            last_timeout_at     TEXT,
            dispute_window_hours INTEGER NOT NULL DEFAULT 72 CHECK(dispute_window_hours >= 1),
            dispute_outcome      TEXT,
            judge_agent_id       TEXT,
            judge_verdict        TEXT,
            quality_score        INTEGER,
            callback_url         TEXT,
            callback_secret      TEXT,
            batch_id             TEXT
        )
    """)


def _create_job_messages_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_messages (
            message_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id       TEXT NOT NULL,
            from_id      TEXT NOT NULL,
            type         TEXT NOT NULL,
            payload      TEXT NOT NULL,
            correlation_id TEXT,
            created_at   TEXT NOT NULL
        )
    """)


def _job_messages_columns(conn: sqlite3.Connection) -> dict:
    return {
        row["name"]: row
        for row in conn.execute("PRAGMA table_info(job_messages)").fetchall()
    }


def _ensure_job_messages_schema(conn: sqlite3.Connection) -> None:
    _create_job_messages_table(conn)
    cols = _job_messages_columns(conn)
    if "correlation_id" not in cols:
        conn.execute("ALTER TABLE job_messages ADD COLUMN correlation_id TEXT")


def _ensure_jobs_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_caller ON jobs(caller_owner_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_agent ON jobs(agent_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_agent_owner ON jobs(agent_owner_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_retry_due ON jobs(next_retry_at, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_lease_due ON jobs(lease_expires_at, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_claim_owner ON jobs(claim_owner_id, updated_at DESC)"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_caller_status_created_job
        ON jobs(caller_owner_id, status, created_at DESC, job_id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_agent_status_created_job
        ON jobs(agent_id, status, created_at DESC, job_id DESC)
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_batch_created ON jobs(batch_id, created_at DESC)"
    )


def _needs_jobs_migration(conn: sqlite3.Connection) -> bool:
    cols = _jobs_columns(conn)
    if not _REQUIRED_JOB_COLUMNS.issubset(cols.keys()):
        return True
    if cols["job_id"]["pk"] != 1:
        return True
    if cols["agent_owner_id"]["notnull"] != 1:
        return True
    if cols["attempt_count"]["dflt_value"] != "0":
        return True
    if cols["max_attempts"]["dflt_value"] != "3":
        return True
    if cols["retry_count"]["dflt_value"] != "0":
        return True
    if cols["timeout_count"]["dflt_value"] != "0":
        return True
    return False


def _normalize_legacy_job_row(row: dict, used_job_ids: set[str]) -> tuple:
    legacy_rowid = row.get("_legacy_rowid", 0)

    raw_job_id = _clean_optional_text(row.get("job_id"))
    if not raw_job_id:
        raw_job_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"legacy-job:{legacy_rowid}:{row.get('agent_id') or ''}:{row.get('created_at') or ''}",
            )
        )

    job_id = raw_job_id
    suffix = 2
    while job_id in used_job_ids:
        job_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{raw_job_id}:{legacy_rowid}:{suffix}",
            )
        )
        suffix += 1
    used_job_ids.add(job_id)

    agent_id = _clean_optional_text(row.get("agent_id")) or "legacy-agent"
    agent_owner_id = _clean_optional_text(row.get("agent_owner_id")) or f"agent:{agent_id}"
    caller_owner_id = _clean_optional_text(row.get("caller_owner_id")) or f"legacy-caller:{job_id}"
    caller_wallet_id = _clean_optional_text(row.get("caller_wallet_id")) or f"legacy-caller-wallet:{job_id}"
    agent_wallet_id = _clean_optional_text(row.get("agent_wallet_id")) or f"legacy-agent-wallet:{job_id}"
    platform_wallet_id = _clean_optional_text(row.get("platform_wallet_id")) or f"legacy-platform-wallet:{job_id}"

    status = _clean_optional_text(row.get("status")) or "pending"
    if status not in VALID_STATUSES:
        status = "pending"

    price_cents = _to_non_negative_int(row.get("price_cents"), default=0)
    charge_tx_id = _clean_optional_text(row.get("charge_tx_id")) or str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-charge:{job_id}")
    )

    input_payload = _normalize_required_json(row.get("input_payload"), default={})
    output_payload = _normalize_optional_json(row.get("output_payload"))

    error_message = _clean_optional_text(row.get("error_message"))
    created_at = _clean_optional_text(row.get("created_at")) or _CANONICAL_CREATED_AT
    updated_at = _clean_optional_text(row.get("updated_at")) or created_at
    completed_at = _clean_optional_text(row.get("completed_at"))
    settled_at = _clean_optional_text(row.get("settled_at"))

    claim_owner_id = _clean_optional_text(row.get("claim_owner_id"))
    claim_token = _clean_optional_text(row.get("claim_token"))
    claimed_at = _clean_optional_text(row.get("claimed_at"))
    lease_expires_at = _clean_optional_text(row.get("lease_expires_at"))
    last_heartbeat_at = _clean_optional_text(row.get("last_heartbeat_at"))

    attempt_count = _to_non_negative_int(row.get("attempt_count"), default=0)
    max_attempts = max(1, _to_non_negative_int(row.get("max_attempts"), default=3))
    retry_count = _to_non_negative_int(row.get("retry_count"), default=0)
    if retry_count > max_attempts:
        retry_count = max_attempts

    next_retry_at = _clean_optional_text(row.get("next_retry_at"))
    last_retry_at = _clean_optional_text(row.get("last_retry_at"))

    timeout_count = _to_non_negative_int(row.get("timeout_count"), default=0)
    last_timeout_at = _clean_optional_text(row.get("last_timeout_at"))
    dispute_window_hours = max(1, _to_non_negative_int(row.get("dispute_window_hours"), default=72))
    dispute_outcome = _clean_optional_text(row.get("dispute_outcome"))
    judge_agent_id = _clean_optional_text(row.get("judge_agent_id"))
    judge_verdict = _clean_optional_text(row.get("judge_verdict"))
    quality_score = row.get("quality_score")
    try:
        parsed_quality_score = int(quality_score) if quality_score is not None else None
    except (TypeError, ValueError):
        parsed_quality_score = None
    callback_url = _clean_optional_text(row.get("callback_url"))
    callback_secret = _clean_optional_text(row.get("callback_secret"))
    batch_id = _clean_optional_text(row.get("batch_id"))

    if claim_owner_id is None:
        claim_token = None
        claimed_at = None
        lease_expires_at = None
        last_heartbeat_at = None

    if completed_at or settled_at:
        claim_owner_id = None
        claim_token = None
        claimed_at = None
        lease_expires_at = None
        last_heartbeat_at = None
        next_retry_at = None

    return (
        job_id,
        agent_id,
        agent_owner_id,
        caller_owner_id,
        caller_wallet_id,
        agent_wallet_id,
        platform_wallet_id,
        status,
        price_cents,
        charge_tx_id,
        input_payload,
        output_payload,
        error_message,
        created_at,
        updated_at,
        completed_at,
        settled_at,
        claim_owner_id,
        claim_token,
        claimed_at,
        lease_expires_at,
        last_heartbeat_at,
        attempt_count,
        max_attempts,
        retry_count,
        next_retry_at,
        last_retry_at,
        timeout_count,
        last_timeout_at,
        dispute_window_hours,
        dispute_outcome,
        judge_agent_id,
        judge_verdict,
        parsed_quality_score,
        callback_url,
        callback_secret,
        batch_id,
    )


def _migrate_jobs_table(conn: sqlite3.Connection) -> None:
    columns = _jobs_columns(conn)
    order_by = "created_at, rowid" if "created_at" in columns else "rowid"
    legacy_rows = conn.execute(
        f"SELECT rowid AS _legacy_rowid, * FROM jobs ORDER BY {order_by}"
    ).fetchall()

    fk_row = conn.execute("PRAGMA foreign_keys").fetchone()
    fk_enabled = bool(fk_row and int(fk_row[0]) == 1)
    if fk_enabled:
        conn.execute("PRAGMA foreign_keys=OFF")

    try:
        conn.execute("DROP TABLE IF EXISTS jobs__canonical")
        _create_jobs_table(conn, table_name="jobs__canonical")

        cols_sql = ", ".join(_CANONICAL_JOB_COLUMNS)
        placeholders = ", ".join(["?"] * len(_CANONICAL_JOB_COLUMNS))

        used_job_ids: set[str] = set()
        for row in legacy_rows:
            normalized = _normalize_legacy_job_row(dict(row), used_job_ids)
            conn.execute(
                f"INSERT INTO jobs__canonical ({cols_sql}) VALUES ({placeholders})",
                normalized,
            )

        conn.execute("DROP TABLE jobs")
        conn.execute("ALTER TABLE jobs__canonical RENAME TO jobs")
    except Exception:
        conn.execute("DROP TABLE IF EXISTS jobs__canonical")
        raise
    finally:
        if fk_enabled:
            conn.execute("PRAGMA foreign_keys=ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError("jobs migration introduced foreign key violations.")


def init_jobs_db() -> None:
    """Create or migrate jobs tables and indexes."""
    with _conn() as conn:
        if not _jobs_table_exists(conn):
            _create_jobs_table(conn)
        elif _needs_jobs_migration(conn):
            _migrate_jobs_table(conn)
        _ensure_job_messages_schema(conn)
        _ensure_jobs_indexes(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_messages_job ON job_messages(job_id, message_id)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_messages_job_correlation
            ON job_messages(job_id, correlation_id, message_id)
            """
        )


def create_job(
    agent_id: str,
    caller_owner_id: str,
    caller_wallet_id: str,
    agent_wallet_id: str,
    platform_wallet_id: str,
    price_cents: int,
    charge_tx_id: str,
    input_payload: dict,
    agent_owner_id: str | None = None,
    max_attempts: int = 3,
    dispute_window_hours: int = 72,
    judge_agent_id: str | None = None,
    callback_url: str | None = None,
    callback_secret: str | None = None,
    batch_id: str | None = None,
) -> dict:
    if price_cents < 0:
        raise ValueError("price_cents must be non-negative.")

    parsed_max_attempts = _to_non_negative_int(max_attempts, default=0)
    if parsed_max_attempts < 1:
        raise ValueError("max_attempts must be >= 1.")
    parsed_dispute_window_hours = _to_non_negative_int(dispute_window_hours, default=0)
    if parsed_dispute_window_hours < 1:
        raise ValueError("dispute_window_hours must be >= 1.")

    owner_id = (agent_owner_id or f"agent:{agent_id}").strip()
    if not owner_id:
        raise ValueError("agent_owner_id must be a non-empty string.")

    job_id = str(uuid.uuid4())
    now = _now()

    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs
              (job_id, agent_id, agent_owner_id, caller_owner_id, caller_wallet_id,
               agent_wallet_id, platform_wallet_id, status, price_cents, charge_tx_id,
               input_payload, created_at, updated_at, max_attempts, dispute_window_hours, judge_agent_id,
               callback_url, callback_secret, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                agent_id,
                owner_id,
                caller_owner_id,
                caller_wallet_id,
                agent_wallet_id,
                platform_wallet_id,
                "pending",
                price_cents,
                charge_tx_id,
                json.dumps(input_payload),
                now,
                now,
                parsed_max_attempts,
                parsed_dispute_window_hours,
                _clean_optional_text(judge_agent_id),
                _clean_optional_text(callback_url),
                _clean_optional_text(callback_secret),
                _clean_optional_text(batch_id),
            ),
        )
    return get_job(job_id)


def list_jobs_for_batch(batch_id: str, caller_owner_id: str) -> list[dict]:
    normalized_batch_id = _clean_optional_text(batch_id)
    normalized_owner_id = _clean_optional_text(caller_owner_id)
    if normalized_batch_id is None or normalized_owner_id is None:
        return []
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE batch_id = ? AND caller_owner_id = ?
            ORDER BY created_at ASC, job_id ASC
            """,
            (normalized_batch_id, normalized_owner_id),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_job(job_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_job_authorization_context(job_id: str) -> dict | None:
    job = get_job(job_id)
    if job is None:
        return None
    return {
        "job_id": job["job_id"],
        "agent_id": job["agent_id"],
        "agent_owner_id": job["agent_owner_id"],
        "caller_owner_id": job["caller_owner_id"],
        "claim_owner_id": job["claim_owner_id"],
    }


def is_worker_authorized(job: dict, worker_owner_id: str) -> bool:
    expected_owner = (job.get("agent_owner_id") or "").strip()
    candidate = (worker_owner_id or "").strip()
    return bool(expected_owner) and candidate == expected_owner


def is_worker_authorized_for_job(job_id: str, worker_owner_id: str) -> bool:
    job = get_job(job_id)
    if job is None:
        return False
    return is_worker_authorized(job, worker_owner_id)


def list_jobs_for_owner(
    owner_id: str,
    limit: int = 50,
    status: str | None = None,
    before_created_at: str | None = None,
    before_job_id: str | None = None,
) -> list:
    limit = min(max(1, limit), 200)
    where_clauses = ["caller_owner_id = ?"]
    params: list = [owner_id]
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if before_created_at:
        cursor_job_id = before_job_id or "\uffff"
        where_clauses.append("(created_at < ? OR (created_at = ? AND job_id < ?))")
        params.extend([before_created_at, before_created_at, cursor_job_id])
    where_sql = " AND ".join(where_clauses)
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {where_sql}
            ORDER BY created_at DESC, job_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_jobs_for_agent(
    agent_id: str,
    limit: int = 50,
    status: str | None = None,
    before_created_at: str | None = None,
    before_job_id: str | None = None,
) -> list:
    limit = min(max(1, limit), 200)
    where_clauses = ["agent_id = ?"]
    params: list = [agent_id]
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if before_created_at:
        cursor_job_id = before_job_id or "\uffff"
        where_clauses.append("(created_at < ? OR (created_at = ? AND job_id < ?))")
        params.extend([before_created_at, before_created_at, cursor_job_id])
    where_sql = " AND ".join(where_clauses)
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {where_sql}
            ORDER BY created_at DESC, job_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _lease_is_active(job_row: dict, now_dt: datetime) -> bool:
    claim_owner_id = _clean_optional_text(job_row.get("claim_owner_id"))
    if claim_owner_id is None:
        return False
    lease_expires_at = _parse_ts(_clean_optional_text(job_row.get("lease_expires_at")))
    return bool(lease_expires_at and lease_expires_at > now_dt)


def _lease_is_expired(job_row: dict, now_dt: datetime) -> bool:
    claim_owner_id = _clean_optional_text(job_row.get("claim_owner_id"))
    if claim_owner_id is None:
        return False
    lease_expires_at = _parse_ts(_clean_optional_text(job_row.get("lease_expires_at")))
    if lease_expires_at is None:
        return True
    return lease_expires_at <= now_dt


def claim_job(
    job_id: str,
    claim_owner_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    require_authorized_owner: bool = True,
) -> dict | None:
    owner_id = (claim_owner_id or "").strip()
    if not owner_id:
        raise ValueError("claim_owner_id must be a non-empty string.")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be > 0.")

    now_dt = _now_dt()
    now = now_dt.isoformat()
    lease_expires_at = _iso_after_seconds(lease_seconds)

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        raw = dict(row)
        if raw["status"] not in _CLAIMABLE_STATUSES:
            return None
        if raw["settled_at"] or raw["completed_at"]:
            return None
        if raw["status"] == "pending":
            next_retry_at = _parse_ts(_clean_optional_text(raw.get("next_retry_at")))
            if next_retry_at is not None and next_retry_at > now_dt:
                return None
        if require_authorized_owner and not is_worker_authorized(raw, owner_id):
            return None

        current_owner = _clean_optional_text(raw.get("claim_owner_id"))
        lease_active = _lease_is_active(raw, now_dt)

        if lease_active and current_owner != owner_id:
            return None

        same_owner_reclaim = lease_active and current_owner == owner_id
        attempt_count = _to_non_negative_int(raw.get("attempt_count"), default=0)
        max_attempts = max(1, _to_non_negative_int(raw.get("max_attempts"), default=3))

        if not same_owner_reclaim:
            if attempt_count >= max_attempts:
                return None
            attempt_count += 1
            claim_token = str(uuid.uuid4())
            claimed_at = now
        else:
            claim_token = _clean_optional_text(raw.get("claim_token")) or str(uuid.uuid4())
            claimed_at = _clean_optional_text(raw.get("claimed_at")) or now

        next_status = "running" if raw["status"] == "pending" else raw["status"]

        conn.execute(
            """
            UPDATE jobs
            SET status = ?, claim_owner_id = ?, claim_token = ?, claimed_at = ?,
                lease_expires_at = ?, last_heartbeat_at = ?, attempt_count = ?,
                next_retry_at = NULL, updated_at = ?
            WHERE job_id = ?
            """,
            (
                next_status,
                owner_id,
                claim_token,
                claimed_at,
                lease_expires_at,
                now,
                attempt_count,
                now,
                job_id,
            ),
        )
        event_type = "claim_reclaimed" if current_owner == owner_id else "claim_acquired"
        _insert_claim_event_row(
            conn,
            job_id,
            event_type=event_type,
            claim_owner_id=owner_id,
            claim_token=claim_token,
            lease_started_at=now,
            lease_expires_at=lease_expires_at,
            actor_id=owner_id,
            metadata={
                "status_after": next_status,
                "attempt_count": attempt_count,
            },
            created_at=now,
        )

    return get_job(job_id)


def heartbeat_job_lease(
    job_id: str,
    claim_owner_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    claim_token: str | None = None,
    require_authorized_owner: bool = True,
) -> dict | None:
    owner_id = (claim_owner_id or "").strip()
    if not owner_id:
        raise ValueError("claim_owner_id must be a non-empty string.")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be > 0.")

    now_dt = _now_dt()
    now = now_dt.isoformat()
    lease_expires_at = _iso_after_seconds(lease_seconds)

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        raw = dict(row)
        if require_authorized_owner and not is_worker_authorized(raw, owner_id):
            return None
        if raw["status"] not in _ACTIVE_LEASE_STATUSES:
            return None
        if _clean_optional_text(raw.get("claim_owner_id")) != owner_id:
            return None
        if claim_token is not None and _clean_optional_text(raw.get("claim_token")) != claim_token:
            return None

        existing_expiry = _parse_ts(_clean_optional_text(raw.get("lease_expires_at")))
        if existing_expiry is None or existing_expiry <= now_dt:
            return None

        result = conn.execute(
            """
            UPDATE jobs
            SET lease_expires_at = CASE
                    WHEN lease_expires_at > ? THEN lease_expires_at
                    ELSE ?
                END,
                last_heartbeat_at = ?,
                updated_at = ?
            WHERE job_id = ?
              AND status IN ('running', 'awaiting_clarification')
              AND claim_owner_id = ?
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at > ?
            """,
            (
                lease_expires_at,
                lease_expires_at,
                now,
                now,
                job_id,
                owner_id,
                now,
            ),
        )
        if result.rowcount == 0:
            return None

        new_expiry = _parse_ts(lease_expires_at)
        if existing_expiry and new_expiry and existing_expiry > new_expiry:
            effective_expiry = existing_expiry.isoformat()
        else:
            effective_expiry = lease_expires_at
        _insert_claim_event_row(
            conn,
            job_id,
            event_type="claim_heartbeat",
            claim_owner_id=owner_id,
            claim_token=_clean_optional_text(raw.get("claim_token")),
            lease_started_at=now,
            lease_expires_at=effective_expiry,
            actor_id=owner_id,
            metadata={"lease_seconds": lease_seconds},
            created_at=now,
        )

    return get_job(job_id)


def release_job_claim(
    job_id: str,
    claim_owner_id: str,
    claim_token: str | None = None,
    require_authorized_owner: bool = True,
) -> dict | None:
    owner_id = (claim_owner_id or "").strip()
    if not owner_id:
        raise ValueError("claim_owner_id must be a non-empty string.")

    now = _now()

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        raw = dict(row)
        if require_authorized_owner and not is_worker_authorized(raw, owner_id):
            return None
        if _clean_optional_text(raw.get("claim_owner_id")) != owner_id:
            return None
        if claim_token is not None and _clean_optional_text(raw.get("claim_token")) != claim_token:
            return None

        previous_claim_token = _clean_optional_text(raw.get("claim_token"))
        previous_lease_expires_at = _clean_optional_text(raw.get("lease_expires_at"))
        conn.execute(
            """
            UPDATE jobs
            SET claim_owner_id = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                updated_at = ?
            WHERE job_id = ?
            """,
            (now, job_id),
        )
        _insert_claim_event_row(
            conn,
            job_id,
            event_type="claim_released",
            claim_owner_id=owner_id,
            claim_token=previous_claim_token,
            lease_started_at=now,
            lease_expires_at=previous_lease_expires_at,
            actor_id=owner_id,
            metadata={},
            created_at=now,
        )

    return get_job(job_id)


def schedule_job_retry(
    job_id: str,
    retry_delay_seconds: int,
    error_message: str | None = None,
    claim_owner_id: str | None = None,
    claim_token: str | None = None,
    require_authorized_owner: bool = True,
) -> dict | None:
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds must be >= 0.")

    owner_id = _clean_optional_text(claim_owner_id)
    now_dt = _now_dt()
    now = now_dt.isoformat()

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        raw = dict(row)
        if raw["settled_at"]:
            return None
        if raw["completed_at"]:
            return None

        current_claim_owner = _clean_optional_text(raw.get("claim_owner_id"))
        if owner_id is not None:
            if require_authorized_owner and not is_worker_authorized(raw, owner_id):
                return None
            if current_claim_owner != owner_id:
                return None
        if claim_token is not None and _clean_optional_text(raw.get("claim_token")) != claim_token:
            return None

        attempt_count = _to_non_negative_int(raw.get("attempt_count"), default=0)
        max_attempts = max(1, _to_non_negative_int(raw.get("max_attempts"), default=3))
        retry_count = _to_non_negative_int(raw.get("retry_count"), default=0) + 1

        can_retry = attempt_count < max_attempts
        next_status = "pending" if can_retry else "failed"
        next_retry_at = (now_dt + timedelta(seconds=retry_delay_seconds)).isoformat() if can_retry else None
        completed_at = None if can_retry else (_clean_optional_text(raw.get("completed_at")) or now)
        next_error = error_message if error_message is not None else raw.get("error_message")

        conn.execute(
            """
            UPDATE jobs
            SET status = ?,
                error_message = ?,
                updated_at = ?,
                completed_at = ?,
                claim_owner_id = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                retry_count = ?,
                next_retry_at = ?,
                last_retry_at = ?
            WHERE job_id = ?
            """,
            (
                next_status,
                next_error,
                now,
                completed_at,
                retry_count,
                next_retry_at,
                now,
                job_id,
            ),
        )

    return get_job(job_id)


def mark_job_timeout(
    job_id: str,
    retry_delay_seconds: int = 0,
    error_message: str = "Job lease expired before completion.",
    allow_retry: bool = True,
) -> dict | None:
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds must be >= 0.")

    now_dt = _now_dt()
    now = now_dt.isoformat()

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        raw = dict(row)
        if raw["settled_at"]:
            return None
        if raw["status"] not in _ACTIVE_LEASE_STATUSES:
            return None
        if not _lease_is_expired(raw, now_dt):
            return None

        previous_claim_owner_id = _clean_optional_text(raw.get("claim_owner_id"))
        previous_claim_token = _clean_optional_text(raw.get("claim_token"))
        previous_lease_expires_at = _clean_optional_text(raw.get("lease_expires_at"))
        attempt_count = _to_non_negative_int(raw.get("attempt_count"), default=0)
        max_attempts = max(1, _to_non_negative_int(raw.get("max_attempts"), default=3))
        retry_count = _to_non_negative_int(raw.get("retry_count"), default=0) + 1
        timeout_count = _to_non_negative_int(raw.get("timeout_count"), default=0) + 1

        can_retry = allow_retry and attempt_count < max_attempts
        next_status = "pending" if can_retry else "failed"
        next_retry_at = (now_dt + timedelta(seconds=retry_delay_seconds)).isoformat() if can_retry else None
        completed_at = None if can_retry else (_clean_optional_text(raw.get("completed_at")) or now)

        conn.execute(
            """
            UPDATE jobs
            SET status = ?,
                error_message = ?,
                updated_at = ?,
                completed_at = ?,
                claim_owner_id = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                retry_count = ?,
                next_retry_at = ?,
                last_retry_at = ?,
                timeout_count = ?,
                last_timeout_at = ?
            WHERE job_id = ?
            """,
            (
                next_status,
                error_message,
                now,
                completed_at,
                retry_count,
                next_retry_at,
                now,
                timeout_count,
                now,
                job_id,
            ),
        )
        _insert_claim_event_row(
            conn,
            job_id,
            event_type="claim_timed_out",
            claim_owner_id=previous_claim_owner_id,
            claim_token=previous_claim_token,
            lease_started_at=now,
            lease_expires_at=previous_lease_expires_at,
            metadata={
                "status_after": next_status,
                "retry_count": retry_count,
                "timeout_count": timeout_count,
                "allow_retry": bool(allow_retry),
            },
            created_at=now,
        )

    return get_job(job_id)


def list_jobs_due_for_retry(limit: int = 100, now: str | None = None) -> list:
    limit = min(max(1, limit), 200)
    now_iso = now or _now()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'pending'
              AND completed_at IS NULL
              AND settled_at IS NULL
              AND next_retry_at IS NOT NULL
              AND next_retry_at <= ?
              AND retry_count < max_attempts
            ORDER BY next_retry_at ASC, created_at ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_retry_ready(job_id: str, now: str | None = None) -> dict | None:
    """
    Clear retry scheduling + lease claim fields so a pending retry becomes claimable.
    Returns the updated job, or None if the row was not due for retry.
    """
    now_iso = now or _now()
    with _conn() as conn:
        result = conn.execute(
            """
            UPDATE jobs
            SET next_retry_at = NULL,
                last_retry_at = NULL,
                claim_owner_id = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                updated_at = ?
            WHERE job_id = ?
              AND status = 'pending'
              AND next_retry_at IS NOT NULL
              AND next_retry_at <= ?
            """,
            (now_iso, job_id, now_iso),
        )
    if result.rowcount == 0:
        return None
    return get_job(job_id)


def list_jobs_with_expired_leases(limit: int = 100, now: str | None = None) -> list:
    limit = min(max(1, limit), 200)
    now_iso = now or _now()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status IN ('running', 'awaiting_clarification')
              AND completed_at IS NULL
              AND settled_at IS NULL
              AND claim_owner_id IS NOT NULL
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            ORDER BY lease_expires_at ASC, created_at ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_jobs_past_sla(sla_seconds: int, limit: int = 100, now: str | None = None) -> list:
    if sla_seconds <= 0:
        raise ValueError("sla_seconds must be > 0.")
    limit = min(max(1, limit), 200)
    now_dt = _parse_ts(now or _now()) or _now_dt()
    threshold = (now_dt - timedelta(seconds=sla_seconds)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status IN ('pending', 'running', 'awaiting_clarification')
              AND completed_at IS NULL
              AND settled_at IS NULL
              AND created_at <= ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (threshold, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_job_status(
    job_id: str,
    status: str,
    output_payload: dict | None = None,
    error_message: str | None = None,
    completed: bool = False,
) -> dict | None:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    now = _now()
    clear_claim = 1 if completed else 0
    clear_retry_schedule = 1 if status != "pending" else 0
    completed_flag = 1 if completed else 0

    with _conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, output_payload = ?, error_message = ?,
                updated_at = ?, completed_at = COALESCE(completed_at, ?),
                next_retry_at = CASE WHEN ? = 1 THEN NULL ELSE next_retry_at END,
                claim_owner_id = CASE WHEN ? = 1 THEN NULL ELSE claim_owner_id END,
                claim_token = CASE WHEN ? = 1 THEN NULL ELSE claim_token END,
                lease_expires_at = CASE WHEN ? = 1 THEN NULL ELSE lease_expires_at END,
                last_heartbeat_at = CASE WHEN ? = 1 THEN NULL ELSE last_heartbeat_at END
            WHERE job_id = ? AND (? = 0 OR completed_at IS NULL)
            """,
            (
                status,
                json.dumps(output_payload) if output_payload is not None else None,
                error_message,
                now,
                now if completed else None,
                clear_retry_schedule,
                clear_claim,
                clear_claim,
                clear_claim,
                clear_claim,
                job_id,
                completed_flag,
            ),
        )
    return get_job(job_id)


def mark_settled(job_id: str) -> bool:
    now = _now()
    with _conn() as conn:
        result = conn.execute(
            """
            UPDATE jobs
            SET settled_at = ?
            WHERE job_id = ? AND settled_at IS NULL
            """,
            (now, job_id),
        )
    return result.rowcount > 0


def _message_correlation_exists_conn(
    conn: sqlite3.Connection,
    job_id: str,
    correlation_id: str,
    msg_type: str | None = None,
) -> bool:
    correlation = _clean_optional_text(correlation_id)
    if correlation is None:
        return False
    if msg_type is not None:
        row = conn.execute(
            """
            SELECT 1
            FROM job_messages
            WHERE job_id = ? AND correlation_id = ? AND type = ?
            LIMIT 1
            """,
            (job_id, correlation, msg_type),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1
            FROM job_messages
            WHERE job_id = ? AND correlation_id = ?
            LIMIT 1
            """,
            (job_id, correlation),
        ).fetchone()
    return row is not None


def message_correlation_exists(
    job_id: str,
    correlation_id: str,
    msg_type: str | None = None,
) -> bool:
    with _conn() as conn:
        return _message_correlation_exists_conn(
            conn,
            job_id=job_id,
            correlation_id=correlation_id,
            msg_type=msg_type,
        )


def tool_call_correlation_exists(job_id: str, correlation_id: str) -> bool:
    return message_correlation_exists(job_id, correlation_id, msg_type="tool_call")


def _resolve_message_lease_behavior(raw_type: str, canonical_type: str) -> str | None:
    if canonical_type in MESSAGE_TYPE_LEASE_BEHAVIOR:
        return MESSAGE_TYPE_LEASE_BEHAVIOR[canonical_type]
    return _LEGACY_MESSAGE_TYPE_LEASE_BEHAVIOR.get(raw_type)


def add_message(
    job_id: str,
    from_id: str,
    msg_type: str,
    payload: dict,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    correlation_id: str | None = None,
) -> dict:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be > 0.")

    normalized = _models.normalize_job_message_body(
        msg_type=msg_type,
        payload=payload,
        correlation_id=correlation_id,
        allow_legacy=True,
    )
    normalized_type = normalized["type"]
    canonical_type = normalized["canonical_type"]
    normalized_payload = normalized["payload"]
    normalized_correlation_id = _clean_optional_text(normalized.get("correlation_id"))
    lease_behavior = _resolve_message_lease_behavior(normalized_type, canonical_type)

    should_extend_lease = lease_behavior in {
        _LEASE_BEHAVIOR_EXTEND,
        _LEASE_BEHAVIOR_EXTEND_AND_MARK_AWAITING,
        _LEASE_BEHAVIOR_EXTEND_AND_RESUME_RUNNING,
    }
    status_target: str | None = None
    if lease_behavior == _LEASE_BEHAVIOR_EXTEND_AND_MARK_AWAITING:
        status_target = "awaiting_clarification"
    elif lease_behavior == _LEASE_BEHAVIOR_EXTEND_AND_RESUME_RUNNING:
        status_target = "running"

    now_dt = _now_dt()
    now = now_dt.isoformat()

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if canonical_type == "tool_result":
            if normalized_correlation_id is None:
                raise ValueError("tool_result messages require a correlation_id.")
            if not _message_correlation_exists_conn(
                conn,
                job_id=job_id,
                correlation_id=normalized_correlation_id,
                msg_type="tool_call",
            ):
                raise ValueError(
                    f"tool_result correlation_id '{normalized_correlation_id}' has no matching tool_call."
                )

        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        message_id = _insert_job_message_row(
            conn,
            job_id=job_id,
            from_id=from_id,
            msg_type=normalized_type,
            payload=normalized_payload,
            correlation_id=normalized_correlation_id,
            created_at=now,
        )

        if row is not None and (should_extend_lease or status_target is not None):
            raw = dict(row)
            completed = _clean_optional_text(raw.get("completed_at")) is not None
            settled = _clean_optional_text(raw.get("settled_at")) is not None
            should_update_status = status_target is not None and not completed and not settled

            claim_owner_id = _clean_optional_text(raw.get("claim_owner_id"))
            claim_token = _clean_optional_text(raw.get("claim_token"))
            lease_extended = (
                should_extend_lease and claim_owner_id is not None and claim_token is not None
            )
            next_lease_expires_at = None
            if lease_extended:
                existing_expiry = _parse_ts(_clean_optional_text(raw.get("lease_expires_at")))
                base_dt = existing_expiry if existing_expiry and existing_expiry > now_dt else now_dt
                next_lease_expires_at = (base_dt + timedelta(seconds=lease_seconds)).isoformat()

            if should_update_status or lease_extended:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = CASE WHEN ? = 1 THEN ? ELSE status END,
                        lease_expires_at = CASE WHEN ? = 1 THEN ? ELSE lease_expires_at END,
                        last_heartbeat_at = CASE WHEN ? = 1 THEN ? ELSE last_heartbeat_at END,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        1 if should_update_status else 0,
                        status_target,
                        1 if lease_extended else 0,
                        next_lease_expires_at,
                        1 if lease_extended else 0,
                        now,
                        now,
                        job_id,
                    ),
                )

            if lease_extended:
                _insert_claim_event_row(
                    conn,
                    job_id,
                    event_type="claim_lease_extended",
                    claim_owner_id=claim_owner_id,
                    claim_token=claim_token,
                    lease_started_at=now,
                    lease_expires_at=next_lease_expires_at,
                    actor_id=from_id,
                    metadata={
                        "message_type": normalized_type,
                        "canonical_message_type": canonical_type,
                        "status_after": status_target if should_update_status else raw.get("status"),
                    },
                    created_at=now,
                )

    message = get_message(job_id, message_id)
    _publish_job_message(job_id, message)
    return message


def add_claim_event(
    job_id: str,
    event_type: str,
    *,
    claim_owner_id: str | None = None,
    claim_token: str | None = None,
    lease_expires_at: str | None = None,
    actor_id: str | None = None,
    metadata: dict | None = None,
) -> dict | None:
    now = _now()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        message_id = _insert_claim_event_row(
            conn,
            job_id,
            event_type=event_type,
            claim_owner_id=claim_owner_id,
            claim_token=claim_token,
            lease_started_at=now,
            lease_expires_at=lease_expires_at,
            actor_id=actor_id,
            metadata=metadata,
            created_at=now,
        )
    message = get_message(job_id, message_id)
    _publish_job_message(job_id, message)
    return message


def claim_token_was_recently_active(
    job_id: str,
    claim_owner_id: str,
    claim_token: str,
    within_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    owner_id = _clean_optional_text(claim_owner_id)
    token_hash = _claim_token_sha256(claim_token)
    if owner_id is None or token_hash is None or within_seconds <= 0:
        return False

    cutoff = _now_dt() - timedelta(seconds=within_seconds)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT payload
            FROM job_messages
            WHERE job_id = ?
              AND type = ?
            ORDER BY message_id DESC
            LIMIT 500
            """,
            (job_id, _CLAIM_EVENT_MSG_TYPE),
        ).fetchall()

    for row in rows:
        payload = _decode_json(row["payload"], default={})
        if not isinstance(payload, dict):
            continue
        if payload.get("event_type") not in _ACTIVE_CLAIM_EVENT_TYPES:
            continue
        if payload.get("claim_token_sha256") != token_hash:
            continue
        if _clean_optional_text(payload.get("claim_owner_id")) != owner_id:
            continue
        lease_expires_at = _parse_ts(_clean_optional_text(payload.get("lease_expires_at")))
        if lease_expires_at is None:
            continue
        if lease_expires_at >= cutoff:
            return True
    return False


def get_message(job_id: str, message_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM job_messages
            WHERE job_id = ? AND message_id = ?
            """,
            (job_id, message_id),
        ).fetchone()
    return _msg_to_dict(row) if row else None


def get_messages(job_id: str, since_id: int | None = None, limit: int = 100) -> list:
    limit = min(max(1, limit), 200)
    with _conn() as conn:
        if since_id is not None:
            rows = conn.execute(
                """
                SELECT * FROM job_messages
                WHERE job_id = ? AND message_id > ?
                ORDER BY message_id ASC
                LIMIT ?
                """,
                (job_id, since_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM job_messages
                WHERE job_id = ?
                ORDER BY message_id ASC
                LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
    return [_msg_to_dict(r) for r in rows]


def get_latest_message_id(job_id: str) -> int | None:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT MAX(message_id) AS latest_message_id
            FROM job_messages
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    latest = row["latest_message_id"]
    return int(latest) if latest is not None else None


def _decode_json(raw, default):
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["input_payload"] = _decode_json(d.get("input_payload"), default={})
    d["output_payload"] = _decode_json(d.get("output_payload"), default=None)
    return d


def set_job_quality_result(job_id: str, *, judge_verdict: str, quality_score: int | None, judge_agent_id: str | None) -> dict | None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET judge_verdict = ?, quality_score = ?, judge_agent_id = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (
                _clean_optional_text(judge_verdict),
                int(quality_score) if quality_score is not None else None,
                _clean_optional_text(judge_agent_id),
                now,
                job_id,
            ),
        )
    return get_job(job_id)


def set_job_dispute_outcome(job_id: str, outcome: str | None) -> dict | None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET dispute_outcome = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (_clean_optional_text(outcome), now, job_id),
        )
    return get_job(job_id)


def _msg_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["payload"] = _decode_json(d.get("payload"), default={})
    return d
