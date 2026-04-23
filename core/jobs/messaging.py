"""Job messages, correlation, and quality/dispute fields."""
from __future__ import annotations

from core import models as _models

from .crud import get_job
from .leases import (
    _message_correlation_exists_conn,
    _resolve_message_lease_behavior,
    message_correlation_exists,
    tool_call_correlation_exists,
)
from .db import (
    DEFAULT_LEASE_SECONDS,
    _ACTIVE_CLAIM_EVENT_TYPES,
    _CLAIM_EVENT_MSG_TYPE,
    _LEASE_BEHAVIOR_EXTEND,
    _LEASE_BEHAVIOR_EXTEND_AND_MARK_AWAITING,
    _LEASE_BEHAVIOR_EXTEND_AND_RESUME_RUNNING,
    _claim_token_sha256,
    _clean_optional_text,
    _conn,
    _decode_json,
    _insert_claim_event_row,
    _insert_job_message_row,
    _iso_after_seconds,
    _models,
    _msg_to_dict,
    _now,
    _now_dt,
    _parse_ts,
    _publish_job_message,
    _row_to_dict,
    _to_non_negative_int,
    json,
    sqlite3,
    timedelta,
)
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
            mark_clarification_requested = canonical_type == "clarification_request"
            clear_clarification_tracking = canonical_type == "clarification_response"

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

            clarification_deadline_at = None
            if mark_clarification_requested:
                timeout_seconds = _to_non_negative_int(
                    raw.get("clarification_timeout_seconds"),
                    default=0,
                )
                if timeout_seconds > 0:
                    clarification_deadline_at = (now_dt + timedelta(seconds=timeout_seconds)).isoformat()

            if should_update_status or lease_extended or mark_clarification_requested or clear_clarification_tracking:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = CASE WHEN ? = 1 THEN ? ELSE status END,
                        lease_expires_at = CASE WHEN ? = 1 THEN ? ELSE lease_expires_at END,
                        last_heartbeat_at = CASE WHEN ? = 1 THEN ? ELSE last_heartbeat_at END,
                        clarification_requested_at = CASE
                            WHEN ? = 1 THEN ?
                            WHEN ? = 1 THEN NULL
                            ELSE clarification_requested_at
                        END,
                        clarification_deadline_at = CASE
                            WHEN ? = 1 THEN ?
                            WHEN ? = 1 THEN NULL
                            ELSE clarification_deadline_at
                        END,
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
                        1 if mark_clarification_requested else 0,
                        now,
                        1 if clear_clarification_tracking else 0,
                        1 if mark_clarification_requested else 0,
                        clarification_deadline_at,
                        1 if clear_clarification_tracking else 0,
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


def get_messages(
    job_id: str,
    since_id: int | None = None,
    limit: int = 100,
    msg_type: str | None = None,
    from_id: str | None = None,
    channel: str | None = None,
    to_id: str | None = None,
) -> list:
    limit = min(max(1, limit), 200)
    filters: list[str] = ["job_id = ?"]
    params: list[object] = [job_id]
    if since_id is not None:
        filters.append("message_id > ?")
        params.append(since_id)
    normalized_type = (msg_type or "").strip().lower()
    if normalized_type:
        filters.append("type = ?")
        params.append(normalized_type)
    normalized_from_id = (from_id or "").strip()
    if normalized_from_id:
        filters.append("from_id = ?")
        params.append(normalized_from_id)
    where_sql = " AND ".join(filters)
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM job_messages
            WHERE {where_sql}
            ORDER BY message_id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    messages = [_msg_to_dict(r) for r in rows]
    normalized_channel = (channel or "").strip().lower()
    normalized_to_id = (to_id or "").strip()
    if not normalized_channel and not normalized_to_id:
        return messages

    filtered: list[dict] = []
    for message in messages:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            continue
        if normalized_channel:
            payload_channel = str(payload.get("channel") or "").strip().lower()
            if payload_channel != normalized_channel:
                continue
        if normalized_to_id:
            payload_to_id = str(payload.get("to_id") or "").strip()
            if payload_to_id != normalized_to_id:
                continue
        filtered.append(message)
    return filtered


def count_job_messages(job_id: str) -> int:
    """Return the total number of messages on a job."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM job_messages WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def count_open_clarification_requests(job_id: str) -> int:
    """Count unanswered clarification_request messages on a job."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM job_messages WHERE job_id = ? AND type = 'clarification_request'",
            (job_id,),
        ).fetchone()
    answered = conn.execute(
        "SELECT COUNT(*) AS n FROM job_messages WHERE job_id = ? AND type = 'clarification_response'",
        (job_id,),
    ).fetchone() if conn else None
    requests = int(row["n"]) if row else 0
    responses = int(answered["n"]) if answered else 0
    return max(0, requests - responses)


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

