"""Result-cache helpers for trusted agent outputs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from core import db as _db
from core import reputation

DB_PATH = _db.DB_PATH
_local = _db._local


def _resolved_db_path() -> str:
    module = sys.modules.get("core.cache")
    if module is not None:
        candidate = getattr(module, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return DB_PATH


def _conn() -> sqlite3.Connection:
    return _db.get_raw_connection(_resolved_db_path())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def init_cache_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_result_cache (
                cache_key     TEXT PRIMARY KEY,
                agent_id      TEXT NOT NULL,
                output_json   TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                job_id        TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_agent ON agent_result_cache(agent_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON agent_result_cache(expires_at)")


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def cache_key(agent_id: str, input_payload: Any) -> str:
    canonical = f"{str(agent_id).strip()}:{_canonical_json(input_payload)}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _current_trust_score(agent_id: str) -> float:
    return float(reputation.compute_trust_metrics(agent_id).get("trust_score") or 0.0)


def get_cached(agent_id: str, input_payload: Any) -> Any | None:
    init_cache_db()
    key = cache_key(agent_id, input_payload)
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT output_json, expires_at
            FROM agent_result_cache
            WHERE cache_key = ?
            """,
            (key,),
        ).fetchone()
        if row is None:
            return None
        expires_at = str(row["expires_at"] or "").strip()
        if expires_at and expires_at <= _now().isoformat():
            conn.execute("DELETE FROM agent_result_cache WHERE cache_key = ?", (key,))
            return None
        try:
            return json.loads(row["output_json"])
        except (json.JSONDecodeError, TypeError):
            conn.execute("DELETE FROM agent_result_cache WHERE cache_key = ?", (key,))
            return None


def set_cached(
    agent_id: str,
    input_payload: Any,
    output_payload: Any,
    job_id: str,
    ttl_hours: int = 24,
) -> bool:
    init_cache_db()
    if _current_trust_score(agent_id) < 80.0:
        return False
    ttl = max(1, min(int(ttl_hours or 24), 168))
    key = cache_key(agent_id, input_payload)
    created_at = _now()
    expires_at = created_at + timedelta(hours=ttl)
    output_json = json.dumps(output_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agent_result_cache (cache_key, agent_id, output_json, created_at, expires_at, job_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                agent_id = excluded.agent_id,
                output_json = excluded.output_json,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                job_id = excluded.job_id
            """,
            (key, agent_id, output_json, created_at.isoformat(), expires_at.isoformat(), str(job_id).strip()),
        )
    return True


def evict_expired() -> int:
    init_cache_db()
    with _conn() as conn:
        result = conn.execute(
            "DELETE FROM agent_result_cache WHERE expires_at < ?",
            (_now().isoformat(),),
        )
    return int(result.rowcount or 0)
