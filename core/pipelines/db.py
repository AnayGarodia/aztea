"""SQLite storage helpers for pipelines and pipeline runs."""

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
    pkg = sys.modules.get("core.pipelines")
    if pkg is not None:
        candidate = getattr(pkg, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return DB_PATH


def _conn() -> sqlite3.Connection:
    return _db.get_raw_connection(_resolved_db_path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pipelines (
                pipeline_id   TEXT PRIMARY KEY,
                owner_id      TEXT NOT NULL,
                name          TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                definition    TEXT NOT NULL,
                is_public     INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                run_id          TEXT PRIMARY KEY,
                pipeline_id     TEXT NOT NULL REFERENCES pipelines(pipeline_id),
                caller_owner_id TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'running',
                input_json      TEXT NOT NULL,
                output_json     TEXT,
                error_message   TEXT,
                step_results    TEXT NOT NULL DEFAULT '{}',
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                completed_at    TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipelines_owner_updated ON pipelines(owner_id, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipelines_public_updated ON pipelines(is_public, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_pipeline_created ON pipeline_runs(pipeline_id, created_at DESC)"
        )


def _decode_json(raw: str | None, default):
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _pipeline_row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    payload = dict(row)
    payload["definition"] = _decode_json(payload.get("definition"), {})
    payload["is_public"] = bool(payload.get("is_public"))
    return payload


def _run_row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    payload = dict(row)
    payload["input_payload"] = _decode_json(payload.pop("input_json", None), {})
    payload["output_payload"] = _decode_json(payload.pop("output_json", None), None)
    payload["step_results"] = _decode_json(payload.get("step_results"), {})
    return payload


def create_pipeline(
    owner_id: str,
    name: str,
    definition: dict,
    *,
    description: str = "",
    is_public: bool = False,
    pipeline_id: str | None = None,
) -> dict:
    init_db()
    now = _now()
    generated_id = str(pipeline_id or uuid.uuid4()).strip() or str(uuid.uuid4())
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO pipelines (
                pipeline_id,
                owner_id,
                name,
                description,
                definition,
                is_public,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generated_id,
                str(owner_id).strip(),
                str(name).strip(),
                str(description or "").strip(),
                json.dumps(definition, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                1 if is_public else 0,
                now,
                now,
            ),
        )
    created = get_pipeline(generated_id)
    if created is None:
        raise RuntimeError("Failed to create pipeline.")
    return created


def upsert_pipeline(
    owner_id: str,
    name: str,
    definition: dict,
    *,
    description: str = "",
    is_public: bool = False,
    pipeline_id: str,
) -> dict:
    init_db()
    now = _now()
    normalized_id = str(pipeline_id).strip()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO pipelines (
                pipeline_id,
                owner_id,
                name,
                description,
                definition,
                is_public,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pipeline_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                name = excluded.name,
                description = excluded.description,
                definition = excluded.definition,
                is_public = excluded.is_public,
                updated_at = excluded.updated_at
            """,
            (
                normalized_id,
                str(owner_id).strip(),
                str(name).strip(),
                str(description or "").strip(),
                json.dumps(definition, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                1 if is_public else 0,
                now,
                now,
            ),
        )
    updated = get_pipeline(normalized_id)
    if updated is None:
        raise RuntimeError("Failed to upsert pipeline.")
    return updated


def get_pipeline(pipeline_id: str) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM pipelines WHERE pipeline_id = ?",
            (str(pipeline_id).strip(),),
        ).fetchone()
    return _pipeline_row_to_dict(row)


def list_pipelines(owner_id: str | None = None, *, include_public: bool = False) -> list[dict]:
    init_db()
    clauses: list[str] = []
    params: list[object] = []
    normalized_owner = str(owner_id or "").strip() or None
    if normalized_owner and include_public:
        clauses.append("(owner_id = ? OR is_public = 1)")
        params.append(normalized_owner)
    elif normalized_owner:
        clauses.append("owner_id = ?")
        params.append(normalized_owner)
    elif include_public:
        clauses.append("is_public = 1")
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM pipelines
            {where_sql}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 500
            """,
            tuple(params),
        ).fetchall()
    return [_pipeline_row_to_dict(row) for row in rows if row is not None]


def create_run(
    pipeline_id: str,
    caller_owner_id: str,
    input_payload: dict,
) -> dict:
    init_db()
    run_id = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs (
                run_id,
                pipeline_id,
                caller_owner_id,
                status,
                input_json,
                step_results,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'running', ?, '{}', ?, ?)
            """,
            (
                run_id,
                str(pipeline_id).strip(),
                str(caller_owner_id).strip(),
                json.dumps(input_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                now,
                now,
            ),
        )
    created = get_run(run_id)
    if created is None:
        raise RuntimeError("Failed to create pipeline run.")
    return created


def get_run(run_id: str) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?",
            (str(run_id).strip(),),
        ).fetchone()
    return _run_row_to_dict(row)


def update_run_step(run_id: str, node_id: str, output_payload) -> dict | None:
    init_db()
    now = _now()
    normalized_run_id = str(run_id).strip()
    normalized_node_id = str(node_id).strip()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT step_results FROM pipeline_runs WHERE run_id = ?",
            (normalized_run_id,),
        ).fetchone()
        if row is None:
            return None
        step_results = _decode_json(row["step_results"], {})
        if not isinstance(step_results, dict):
            step_results = {}
        step_results[normalized_node_id] = output_payload
        conn.execute(
            """
            UPDATE pipeline_runs
            SET step_results = ?, updated_at = COALESCE(updated_at, ?)
            WHERE run_id = ?
            """,
            (
                json.dumps(step_results, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                now,
                normalized_run_id,
            ),
        )
    return get_run(run_id)


def complete_run(run_id: str, output_payload) -> dict | None:
    init_db()
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE pipeline_runs
            SET status = 'complete',
                output_json = ?,
                error_message = NULL,
                updated_at = ?,
                completed_at = COALESCE(completed_at, ?)
            WHERE run_id = ?
            """,
            (
                json.dumps(output_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                now,
                now,
                str(run_id).strip(),
            ),
        )
    return get_run(run_id)


def fail_run(run_id: str, error_message: str) -> dict | None:
    init_db()
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE pipeline_runs
            SET status = 'failed',
                error_message = ?,
                updated_at = ?,
                completed_at = COALESCE(completed_at, ?)
            WHERE run_id = ?
            """,
            (
                str(error_message or "").strip() or "Pipeline execution failed.",
                now,
                now,
                str(run_id).strip(),
            ),
        )
    return get_run(run_id)
