"""Async job persistence layer: schema, connections, JSON helpers.

This module owns the SQLite schema definitions for the ``jobs``,
``job_messages``, and claim-event tables, plus low-level utilities used by the
rest of the ``core.jobs`` package:

- Connection pool wiring (thread-local handles, WAL PRAGMAs, deferred writes)
- JSON encode/decode helpers that tolerate legacy rows written pre-migration
- Row-to-dict projectors for jobs and messages
- Constants and validation helpers for enums that bleed into other layers
  (lease behaviours, claim-event types, fee-bearer policies)

Higher-level operations live in sibling modules:

- ``core.jobs.crud`` — creation, listings, authorisation helpers
- ``core.jobs.leases`` — claim/heartbeat/release/retry lifecycle
- ``core.jobs.messaging`` — typed messages and quality/dispute state writes

Jobs are charged up front (see ``core.payments``), and either paid out or
refunded on terminal status. Messages attach to a job so workers can request
clarifications without holding open HTTP connections.
"""

import json
import hashlib
import queue
import sqlite3
import threading
import sys
import uuid
from datetime import datetime, timedelta, timezone

from core import models as _models
from core import db as _db

DB_PATH = _db.DB_PATH
_local = _db._local


def _resolved_db_path() -> str:
    """Prefer ``core.jobs.DB_PATH`` for isolated tests."""
    pkg = sys.modules.get("core.jobs")
    if pkg is not None:
        c = getattr(pkg, "DB_PATH", None)
        if isinstance(c, str) and c:
            return c
    return DB_PATH

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
    "agent_message": _LEASE_BEHAVIOR_EXTEND,
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

PARENT_CASCADE_POLICIES = {
    "detach",
    "fail_children_on_parent_fail",
}

CLARIFICATION_TIMEOUT_POLICIES = {
    "fail",
    "proceed",
}
FEE_BEARER_POLICIES = {
    "worker",
    "caller",
    "split",
}

OUTPUT_VERIFICATION_STATUSES = {
    "not_required",
    "pending",
    "accepted",
    "rejected",
    "expired",
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
    "caller_charge_cents",
    "platform_fee_pct_at_create",
    "fee_bearer_policy",
    "client_id",
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
    "parent_job_id",
    "tree_depth",
    "parent_cascade_policy",
    "retry_count",
    "next_retry_at",
    "last_retry_at",
    "timeout_count",
    "last_timeout_at",
    "clarification_timeout_seconds",
    "clarification_timeout_policy",
    "clarification_requested_at",
    "clarification_deadline_at",
    "dispute_window_hours",
    "dispute_outcome",
    "judge_agent_id",
    "judge_verdict",
    "quality_score",
    "callback_url",
    "callback_secret",
    "output_verification_window_seconds",
    "output_verification_status",
    "output_verification_deadline_at",
    "output_verification_decided_at",
    "output_verification_decision_owner_id",
    "output_verification_reason",
    "batch_id",
)

_REQUIRED_JOB_COLUMNS = set(_CANONICAL_JOB_COLUMNS)


def _conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL mode."""
    return _db.get_raw_connection(_resolved_db_path())


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


def _normalize_parent_cascade_policy(value: str | None) -> str:
    normalized = str(value or "").strip().lower() or "detach"
    if normalized not in PARENT_CASCADE_POLICIES:
        raise ValueError(
            "parent_cascade_policy must be one of: "
            + ", ".join(sorted(PARENT_CASCADE_POLICIES))
            + "."
        )
    return normalized


def _normalize_clarification_timeout_policy(value: str | None) -> str:
    normalized = str(value or "").strip().lower() or "fail"
    if normalized not in CLARIFICATION_TIMEOUT_POLICIES:
        raise ValueError(
            "clarification_timeout_policy must be one of: "
            + ", ".join(sorted(CLARIFICATION_TIMEOUT_POLICIES))
            + "."
        )
    return normalized


def _normalize_output_verification_status(value: str | None) -> str:
    normalized = str(value or "").strip().lower() or "not_required"
    if normalized not in OUTPUT_VERIFICATION_STATUSES:
        return "not_required"
    return normalized


def _normalize_fee_bearer_policy(value: str | None) -> str:
    normalized = str(value or "").strip().lower() or "caller"
    if normalized not in FEE_BEARER_POLICIES:
        return "caller"
    return normalized


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
            caller_charge_cents INTEGER NOT NULL CHECK(caller_charge_cents >= 0),
            platform_fee_pct_at_create INTEGER NOT NULL DEFAULT 10 CHECK(platform_fee_pct_at_create >= 0 AND platform_fee_pct_at_create <= 100),
            fee_bearer_policy   TEXT NOT NULL DEFAULT 'caller',
            client_id           TEXT,
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
            parent_job_id       TEXT,
            tree_depth          INTEGER NOT NULL DEFAULT 0 CHECK(tree_depth >= 0),
            parent_cascade_policy TEXT NOT NULL DEFAULT 'detach',
            retry_count         INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
            next_retry_at       TEXT,
            last_retry_at       TEXT,
            timeout_count       INTEGER NOT NULL DEFAULT 0 CHECK(timeout_count >= 0),
            last_timeout_at     TEXT,
            clarification_timeout_seconds INTEGER NOT NULL DEFAULT 0 CHECK(clarification_timeout_seconds >= 0),
            clarification_timeout_policy  TEXT NOT NULL DEFAULT 'fail',
            clarification_requested_at    TEXT,
            clarification_deadline_at     TEXT,
            dispute_window_hours INTEGER NOT NULL DEFAULT 72 CHECK(dispute_window_hours >= 1),
            dispute_outcome      TEXT,
            judge_agent_id       TEXT,
            judge_verdict        TEXT,
            quality_score        INTEGER,
            callback_url         TEXT,
            callback_secret      TEXT,
            output_verification_window_seconds INTEGER NOT NULL DEFAULT 0 CHECK(output_verification_window_seconds >= 0),
            output_verification_status         TEXT NOT NULL DEFAULT 'not_required',
            output_verification_deadline_at    TEXT,
            output_verification_decided_at     TEXT,
            output_verification_decision_owner_id TEXT,
            output_verification_reason         TEXT,
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_client_created ON jobs(client_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_parent_created ON jobs(parent_job_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_clarification_deadline ON jobs(status, clarification_deadline_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_output_verification_deadline ON jobs(output_verification_status, output_verification_deadline_at)"
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
    caller_charge_cents = _to_non_negative_int(row.get("caller_charge_cents"), default=price_cents)
    if caller_charge_cents < price_cents:
        caller_charge_cents = price_cents
    platform_fee_pct_at_create = _to_non_negative_int(
        row.get("platform_fee_pct_at_create"),
        default=10,
    )
    if platform_fee_pct_at_create > 100:
        platform_fee_pct_at_create = 100
    fee_bearer_policy = _normalize_fee_bearer_policy(row.get("fee_bearer_policy"))
    client_id = _clean_optional_text(row.get("client_id"))
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
    parent_job_id = _clean_optional_text(row.get("parent_job_id"))
    tree_depth = _to_non_negative_int(row.get("tree_depth"), default=0)
    parent_cascade_policy = _normalize_parent_cascade_policy(row.get("parent_cascade_policy"))
    retry_count = _to_non_negative_int(row.get("retry_count"), default=0)
    if retry_count > max_attempts:
        retry_count = max_attempts

    next_retry_at = _clean_optional_text(row.get("next_retry_at"))
    last_retry_at = _clean_optional_text(row.get("last_retry_at"))

    timeout_count = _to_non_negative_int(row.get("timeout_count"), default=0)
    last_timeout_at = _clean_optional_text(row.get("last_timeout_at"))
    clarification_timeout_seconds = _to_non_negative_int(row.get("clarification_timeout_seconds"), default=0)
    clarification_timeout_policy = _normalize_clarification_timeout_policy(
        row.get("clarification_timeout_policy")
    )
    clarification_requested_at = _clean_optional_text(row.get("clarification_requested_at"))
    clarification_deadline_at = _clean_optional_text(row.get("clarification_deadline_at"))
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
    output_verification_window_seconds = _to_non_negative_int(
        row.get("output_verification_window_seconds"),
        default=0,
    )
    output_verification_status = _normalize_output_verification_status(
        row.get("output_verification_status")
    )
    output_verification_deadline_at = _clean_optional_text(row.get("output_verification_deadline_at"))
    output_verification_decided_at = _clean_optional_text(row.get("output_verification_decided_at"))
    output_verification_decision_owner_id = _clean_optional_text(
        row.get("output_verification_decision_owner_id")
    )
    output_verification_reason = _clean_optional_text(row.get("output_verification_reason"))
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

    if status != "awaiting_clarification":
        clarification_requested_at = None
        clarification_deadline_at = None
    elif clarification_timeout_seconds <= 0:
        clarification_deadline_at = None

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
        caller_charge_cents,
        platform_fee_pct_at_create,
        fee_bearer_policy,
        client_id,
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
        parent_job_id,
        tree_depth,
        parent_cascade_policy,
        retry_count,
        next_retry_at,
        last_retry_at,
        timeout_count,
        last_timeout_at,
        clarification_timeout_seconds,
        clarification_timeout_policy,
        clarification_requested_at,
        clarification_deadline_at,
        dispute_window_hours,
        dispute_outcome,
        judge_agent_id,
        judge_verdict,
        parsed_quality_score,
        callback_url,
        callback_secret,
        output_verification_window_seconds,
        output_verification_status,
        output_verification_deadline_at,
        output_verification_decided_at,
        output_verification_decision_owner_id,
        output_verification_reason,
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


def _ensure_job_signature_columns(conn: sqlite3.Connection) -> None:
    """Add cryptographic-signature columns to the jobs table.

    Mirrors migration 0015_agent_identity.sql for dev/test environments
    that bypass the migration runner.
    """
    extras = [
        "ALTER TABLE jobs ADD COLUMN output_signature TEXT",
        "ALTER TABLE jobs ADD COLUMN output_signature_alg TEXT",
        "ALTER TABLE jobs ADD COLUMN output_signed_by_did TEXT",
        "ALTER TABLE jobs ADD COLUMN output_signed_at TEXT",
    ]
    for ddl in extras:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def init_jobs_db() -> None:
    """Create or migrate jobs tables and indexes."""
    with _conn() as conn:
        if not _jobs_table_exists(conn):
            _create_jobs_table(conn)
        elif _needs_jobs_migration(conn):
            _migrate_jobs_table(conn)
        _ensure_job_messages_schema(conn)
        _ensure_jobs_indexes(conn)
        _ensure_job_signature_columns(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_messages_job ON job_messages(job_id, message_id)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_messages_job_correlation
            ON job_messages(job_id, correlation_id, message_id)
            """
        )

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


def _msg_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["payload"] = _decode_json(d.get("payload"), default={})
    return d
