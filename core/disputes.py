"""
disputes.py — dispute lifecycle and bilateral caller ratings persistence.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "registry.db")
_local = threading.local()

DISPUTE_SIDES = {"caller", "agent"}
DISPUTE_STATUSES = {
    "pending",
    "judging",
    "consensus",
    "tied",
    "resolved",
    "appealed",
    "final",
}
DISPUTE_OUTCOMES = {"caller_wins", "agent_wins", "split", "void"}
JUDGE_KINDS = {"llm_primary", "llm_secondary", "human_admin"}


def _conn() -> sqlite3.Connection:
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


def init_disputes_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS disputes (
                dispute_id           TEXT PRIMARY KEY,
                job_id               TEXT NOT NULL REFERENCES jobs(job_id),
                filed_by_owner_id    TEXT NOT NULL,
                side                 TEXT NOT NULL CHECK(side IN ('caller','agent')),
                reason               TEXT NOT NULL,
                evidence             TEXT,
                status               TEXT NOT NULL CHECK(status IN ('pending','judging','consensus','tied','resolved','appealed','final')),
                outcome              TEXT CHECK(outcome IN ('caller_wins','agent_wins','split','void')),
                split_caller_cents   INTEGER,
                split_agent_cents    INTEGER,
                filed_at             TEXT NOT NULL,
                resolved_at          TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_judgments (
                judgment_id     TEXT PRIMARY KEY,
                dispute_id      TEXT NOT NULL,
                judge_kind      TEXT NOT NULL,
                verdict         TEXT NOT NULL,
                reasoning       TEXT NOT NULL,
                model           TEXT,
                admin_user_id   TEXT,
                created_at      TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS caller_ratings (
                rating_id         TEXT PRIMARY KEY,
                job_id            TEXT NOT NULL UNIQUE,
                caller_owner_id   TEXT NOT NULL,
                agent_owner_id    TEXT NOT NULL,
                rating            INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                comment           TEXT,
                created_at        TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_disputes_job_unique ON disputes(job_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_disputes_status_filed ON disputes(status, filed_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_disputes_filer_filed ON disputes(filed_by_owner_id, filed_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dispute_judgments_dispute_created ON dispute_judgments(dispute_id, created_at ASC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_caller_ratings_caller_created ON caller_ratings(caller_owner_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_caller_ratings_agent_created ON caller_ratings(agent_owner_id, created_at DESC)"
        )


def _row_to_dispute(row: sqlite3.Row) -> dict:
    data = dict(row)
    for field in ("split_caller_cents", "split_agent_cents"):
        value = data.get(field)
        data[field] = int(value) if value is not None else None
    return data


def _validate_side(side: str) -> str:
    normalized = str(side or "").strip().lower()
    if normalized not in DISPUTE_SIDES:
        raise ValueError("side must be either 'caller' or 'agent'.")
    return normalized


def _validate_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized not in DISPUTE_STATUSES:
        raise ValueError("invalid dispute status")
    return normalized


def _validate_outcome(outcome: str | None) -> str | None:
    if outcome is None:
        return None
    normalized = str(outcome or "").strip().lower()
    if normalized not in DISPUTE_OUTCOMES:
        raise ValueError("invalid dispute outcome")
    return normalized


def _validate_split(outcome: str | None, split_caller_cents: int | None, split_agent_cents: int | None) -> tuple[int | None, int | None]:
    if outcome != "split":
        return None, None
    if split_caller_cents is None or split_agent_cents is None:
        raise ValueError("split outcomes require split_caller_cents and split_agent_cents.")
    if split_caller_cents < 0 or split_agent_cents < 0:
        raise ValueError("split amounts must be non-negative.")
    return int(split_caller_cents), int(split_agent_cents)


def create_dispute(
    *,
    job_id: str,
    filed_by_owner_id: str,
    side: str,
    reason: str,
    evidence: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise ValueError("reason must be a non-empty string.")
    dispute_id = str(uuid.uuid4())
    now = _now()
    params = (
        dispute_id,
        job_id,
        str(filed_by_owner_id).strip(),
        _validate_side(side),
        normalized_reason,
        str(evidence).strip() if evidence else None,
        now,
    )

    def _insert(created_conn: sqlite3.Connection) -> dict:
        created_conn.execute(
            """
            INSERT INTO disputes
                (dispute_id, job_id, filed_by_owner_id, side, reason, evidence, status, filed_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            params,
        )
        row = created_conn.execute(
            "SELECT * FROM disputes WHERE dispute_id = ?",
            (dispute_id,),
        ).fetchone()
        return _row_to_dispute(row) if row else {}

    if conn is not None:
        return _insert(conn)
    with _conn() as db_conn:
        return _insert(db_conn)


def get_dispute(dispute_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM disputes WHERE dispute_id = ?",
            (dispute_id,),
        ).fetchone()
    return _row_to_dispute(row) if row else None


def get_dispute_by_job(job_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM disputes WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return _row_to_dispute(row) if row else None


def has_dispute_for_job(job_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM disputes WHERE job_id = ? LIMIT 1",
            (job_id,),
        ).fetchone()
    return row is not None


def list_disputes(*, status: str | None = None, limit: int = 100) -> list[dict]:
    capped = min(max(1, int(limit)), 500)
    with _conn() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT * FROM disputes
                WHERE status = ?
                ORDER BY filed_at DESC
                LIMIT ?
                """,
                (_validate_status(status), capped),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM disputes
                ORDER BY filed_at DESC
                LIMIT ?
                """,
                (capped,),
            ).fetchall()
    return [_row_to_dispute(row) for row in rows]


def set_dispute_status(dispute_id: str, status: str) -> dict | None:
    normalized_status = _validate_status(status)
    with _conn() as conn:
        conn.execute(
            """
            UPDATE disputes
            SET status = ?
            WHERE dispute_id = ?
            """,
            (normalized_status, dispute_id),
        )
    return get_dispute(dispute_id)


def set_dispute_consensus(dispute_id: str, outcome: str) -> dict | None:
    normalized_outcome = _validate_outcome(outcome)
    with _conn() as conn:
        conn.execute(
            """
            UPDATE disputes
            SET status = 'consensus', outcome = ?
            WHERE dispute_id = ?
            """,
            (normalized_outcome, dispute_id),
        )
    return get_dispute(dispute_id)


def finalize_dispute(
    dispute_id: str,
    *,
    status: str,
    outcome: str,
    split_caller_cents: int | None = None,
    split_agent_cents: int | None = None,
) -> dict | None:
    normalized_status = _validate_status(status)
    normalized_outcome = _validate_outcome(outcome)
    caller_split, agent_split = _validate_split(normalized_outcome, split_caller_cents, split_agent_cents)
    resolved_at = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE disputes
            SET status = ?,
                outcome = ?,
                split_caller_cents = ?,
                split_agent_cents = ?,
                resolved_at = ?
            WHERE dispute_id = ?
            """,
            (
                normalized_status,
                normalized_outcome,
                caller_split,
                agent_split,
                resolved_at,
                dispute_id,
            ),
        )
    return get_dispute(dispute_id)


def record_judgment(
    dispute_id: str,
    *,
    judge_kind: str,
    verdict: str,
    reasoning: str,
    model: str | None = None,
    admin_user_id: str | None = None,
) -> dict:
    normalized_kind = str(judge_kind or "").strip().lower()
    if normalized_kind not in JUDGE_KINDS:
        raise ValueError("invalid judge_kind")
    normalized_verdict = _validate_outcome(verdict)
    normalized_reasoning = str(reasoning or "").strip()
    if not normalized_reasoning:
        raise ValueError("reasoning must be a non-empty string.")

    judgment = {
        "judgment_id": str(uuid.uuid4()),
        "dispute_id": dispute_id,
        "judge_kind": normalized_kind,
        "verdict": normalized_verdict,
        "reasoning": normalized_reasoning,
        "model": str(model).strip() if model else None,
        "admin_user_id": str(admin_user_id).strip() if admin_user_id else None,
        "created_at": _now(),
    }

    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO dispute_judgments
                (judgment_id, dispute_id, judge_kind, verdict, reasoning, model, admin_user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                judgment["judgment_id"],
                judgment["dispute_id"],
                judgment["judge_kind"],
                judgment["verdict"],
                judgment["reasoning"],
                judgment["model"],
                judgment["admin_user_id"],
                judgment["created_at"],
            ),
        )
    return judgment


def get_judgments(dispute_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM dispute_judgments
            WHERE dispute_id = ?
            ORDER BY created_at ASC, judgment_id ASC
            """,
            (dispute_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_dispute_context(dispute_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT
                d.*,
                j.job_id AS ctx_job_id,
                j.agent_id,
                j.agent_owner_id,
                j.caller_owner_id,
                j.input_payload,
                j.output_payload,
                j.error_message,
                j.status AS job_status,
                j.price_cents,
                j.charge_tx_id,
                j.completed_at,
                j.settled_at,
                a.input_schema
            FROM disputes d
            JOIN jobs j ON j.job_id = d.job_id
            LEFT JOIN agents a ON a.agent_id = j.agent_id
            WHERE d.dispute_id = ?
            """,
            (dispute_id,),
        ).fetchone()
    if row is None:
        return None

    data = dict(row)
    dispute = {key: data[key] for key in (
        "dispute_id",
        "job_id",
        "filed_by_owner_id",
        "side",
        "reason",
        "evidence",
        "status",
        "outcome",
        "split_caller_cents",
        "split_agent_cents",
        "filed_at",
        "resolved_at",
    )}
    for field in ("split_caller_cents", "split_agent_cents"):
        value = dispute.get(field)
        dispute[field] = int(value) if value is not None else None

    def _parse_json(value: str | None, default):
        if value is None:
            return default
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return default
        return parsed

    return {
        "dispute": dispute,
        "job": {
            "job_id": data["ctx_job_id"],
            "agent_id": data["agent_id"],
            "agent_owner_id": data["agent_owner_id"],
            "caller_owner_id": data["caller_owner_id"],
            "input_payload": _parse_json(data.get("input_payload"), {}),
            "output_payload": _parse_json(data.get("output_payload"), None),
            "error_message": data.get("error_message"),
            "status": data.get("job_status"),
            "price_cents": int(data.get("price_cents") or 0),
            "charge_tx_id": data.get("charge_tx_id"),
            "completed_at": data.get("completed_at"),
            "settled_at": data.get("settled_at"),
        },
        "agent_input_schema": _parse_json(data.get("input_schema"), {}),
        "judgments": get_judgments(dispute_id),
    }
