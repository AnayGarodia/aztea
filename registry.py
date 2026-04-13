"""
registry.py — SQLite-backed agent registry for the agentmarket platform.

Production notes:
  - WAL mode enabled for concurrent read performance under load.
  - Thread-local connections (SQLite is not thread-safe across connections;
    each thread gets its own handle).
  - Indexes on tags and name for fast filtered lookups.
  - input_schema stored as JSON; describes the fields a caller must supply.
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "registry.db")
_local = threading.local()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    """
    Return a thread-local SQLite connection.
    Opens a new one if this thread doesn't have one yet, and enables WAL mode.
    """
    if not getattr(_local, "conn", None):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def init_db() -> None:
    """Create the agents table and indexes if they do not already exist."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                agent_id            TEXT PRIMARY KEY,
                name                TEXT NOT NULL UNIQUE,
                description         TEXT NOT NULL,
                endpoint_url        TEXT NOT NULL,
                price_per_call_usd  REAL NOT NULL CHECK(price_per_call_usd >= 0),
                avg_latency_ms      REAL NOT NULL DEFAULT 0.0,
                total_calls         INTEGER NOT NULL DEFAULT 0,
                successful_calls    INTEGER NOT NULL DEFAULT 0,
                tags                TEXT NOT NULL DEFAULT '[]',
                input_schema        TEXT NOT NULL DEFAULT '{}',
                created_at          TEXT NOT NULL
            )
        """)
        # Migrate: add input_schema if an older DB doesn't have it
        try:
            conn.execute("ALTER TABLE agents ADD COLUMN input_schema TEXT NOT NULL DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass  # column already exists

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agents_name ON agents(name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agents_created ON agents(created_at)"
        )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    d["input_schema"] = json.loads(d.get("input_schema") or "{}")
    total = d["total_calls"]
    successful = d.pop("successful_calls")
    d["success_rate"] = round(successful / total, 4) if total > 0 else 1.0
    return d


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def register_agent(
    name: str,
    description: str,
    endpoint_url: str,
    price_per_call_usd: float,
    tags: list,
    agent_id: str | None = None,
    input_schema: dict | None = None,
) -> str:
    """
    Insert a new agent listing. Returns the agent_id.
    Pass agent_id explicitly for deterministic IDs (e.g. self-registration).
    Raises sqlite3.IntegrityError if agent_id already exists.
    """
    aid = agent_id or str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    schema_json = json.dumps(input_schema or {})
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agents
                (agent_id, name, description, endpoint_url,
                 price_per_call_usd, tags, input_schema, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (aid, name, description, endpoint_url,
             price_per_call_usd, json.dumps(tags), schema_json, created_at),
        )
    return aid


def update_call_stats(agent_id: str, latency_ms: float, success: bool) -> None:
    """
    Increment total_calls, update running avg_latency_ms, and conditionally
    increment successful_calls. Uses a single UPDATE with arithmetic to avoid
    a read-modify-write race.
    """
    with _conn() as conn:
        conn.execute(
            """
            UPDATE agents
            SET total_calls    = total_calls + 1,
                avg_latency_ms = (avg_latency_ms * total_calls + ?) / (total_calls + 1),
                successful_calls = successful_calls + ?
            WHERE agent_id = ?
            """,
            (latency_ms, 1 if success else 0, agent_id),
        )


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_agents(tag: str | None = None) -> list:
    """
    Return all agent listings, optionally filtered by tag.
    Tag matching uses exact JSON-array membership to avoid substring false-positives.
    """
    with _conn() as conn:
        if tag:
            rows = conn.execute(
                "SELECT * FROM agents WHERE tags LIKE ? ORDER BY created_at",
                (f'%"{tag}"%',),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agents ORDER BY created_at"
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_agent(agent_id: str) -> dict | None:
    """Return a single agent listing by ID, or None if not found."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def agent_exists_by_name(name: str) -> bool:
    """Return True if any agent with this name is already registered."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM agents WHERE name = ?", (name,)
        ).fetchone()
    return row is not None
