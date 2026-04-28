"""Persistence helpers for compare-hiring sessions."""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

from core import db as _db

DB_PATH = _db.DB_PATH
_local = _db._local


def _resolved_db_path() -> str:
    module = sys.modules.get("core.compare")
    if module is not None:
        candidate = getattr(module, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return DB_PATH


def _conn() -> sqlite3.Connection:
    return _db.get_raw_connection(_resolved_db_path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    """Create compare_sessions and compare_results tables if they don't exist. Idempotent."""
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS compare_sessions (
                compare_id      TEXT PRIMARY KEY,
                caller_owner_id TEXT NOT NULL,
                input_json      TEXT NOT NULL,
                agent_ids_json  TEXT NOT NULL,
                job_ids_json    TEXT NOT NULL DEFAULT '[]',
                status          TEXT NOT NULL DEFAULT 'running',
                winner_agent_id TEXT,
                created_at      TEXT NOT NULL,
                completed_at    TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_compare_owner_created ON compare_sessions(caller_owner_id, created_at DESC)"
        )


def _decode_json(raw: str | None, default):
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    payload = dict(row)
    payload["agent_ids"] = _decode_json(payload.pop("agent_ids_json", None), [])
    payload["job_ids"] = _decode_json(payload.pop("job_ids_json", None), [])
    payload["input_payload"] = _decode_json(payload.pop("input_json", None), {})
    return payload


def create_compare(
    caller_owner_id: str,
    agent_ids: list[str],
    input_payload: dict,
    *,
    job_ids: list[str],
) -> dict:
    """Insert a new compare session for running the same task across N agents side-by-side.

    Returns the newly created compare session dict.
    """
    init_db()
    compare_id = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO compare_sessions (
                compare_id,
                caller_owner_id,
                input_json,
                agent_ids_json,
                job_ids_json,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                compare_id,
                str(caller_owner_id).strip(),
                json.dumps(input_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                json.dumps(agent_ids, ensure_ascii=True),
                json.dumps(job_ids, ensure_ascii=True),
                now,
            ),
        )
    created = get_compare(compare_id)
    if created is None:
        raise RuntimeError("Failed to create compare session.")
    return created


def get_compare(compare_id: str) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM compare_sessions WHERE compare_id = ?",
            (str(compare_id).strip(),),
        ).fetchone()
    return _row_to_dict(row)


def mark_complete(compare_id: str) -> dict | None:
    """Mark a compare session as complete once all participating jobs have finished."""
    init_db()
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE compare_sessions
            SET status = 'complete',
                completed_at = COALESCE(completed_at, ?)
            WHERE compare_id = ?
            """,
            (now, str(compare_id).strip()),
        )
    return get_compare(compare_id)


def select_winner(compare_id: str, winner_agent_id: str) -> dict | None:
    """Record the caller's chosen winner agent for a compare session. Idempotent if same winner."""
    init_db()
    normalized_compare_id = str(compare_id).strip()
    normalized_winner = str(winner_agent_id).strip()
    now = _now()
    with _conn() as conn:
        row = conn.execute(
            "SELECT winner_agent_id FROM compare_sessions WHERE compare_id = ?",
            (normalized_compare_id,),
        ).fetchone()
        if row is None:
            return None
        current = str(row["winner_agent_id"] or "").strip()
        if current and current != normalized_winner:
            raise ValueError("Compare session winner has already been selected.")
        conn.execute(
            """
            UPDATE compare_sessions
            SET winner_agent_id = COALESCE(winner_agent_id, ?),
                status = 'complete',
                completed_at = COALESCE(completed_at, ?)
            WHERE compare_id = ?
            """,
            (normalized_winner, now, normalized_compare_id),
        )
    return get_compare(compare_id)
