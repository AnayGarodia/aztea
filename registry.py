"""
registry.py — SQLite-backed agent registry for the agentmarket platform.

Stores agent listings (metadata, pricing, live stats) and provides functions
to register agents, query them, and update call statistics after every proxied
invocation. No ORM — raw sqlite3 only.

Schema note: `successful_calls` is stored in the DB but not exposed in the
public dict. `success_rate` is derived on read (successful_calls / total_calls).
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "registry.db")


def _conn() -> sqlite3.Connection:
    """Open a new SQLite connection for the calling thread."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the agents table if it does not already exist."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                agent_id            TEXT PRIMARY KEY,
                name                TEXT NOT NULL,
                description         TEXT NOT NULL,
                endpoint_url        TEXT NOT NULL,
                price_per_call_usd  REAL NOT NULL,
                avg_latency_ms      REAL NOT NULL DEFAULT 0.0,
                total_calls         INTEGER NOT NULL DEFAULT 0,
                successful_calls    INTEGER NOT NULL DEFAULT 0,
                tags                TEXT NOT NULL DEFAULT '[]',
                created_at          TEXT NOT NULL
            )
        """)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    total = d["total_calls"]
    successful = d.pop("successful_calls")  # internal only; not in public schema
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
) -> str:
    """
    Insert a new agent listing. Returns the agent_id.
    Pass agent_id explicitly for deterministic IDs (e.g. self-registration).
    Raises sqlite3.IntegrityError if agent_id already exists.
    """
    aid = agent_id or str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agents
                (agent_id, name, description, endpoint_url,
                 price_per_call_usd, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (aid, name, description, endpoint_url,
             price_per_call_usd, json.dumps(tags), created_at),
        )
    return aid


def update_call_stats(agent_id: str, latency_ms: float, success: bool) -> None:
    """
    Increment total_calls, update running avg_latency_ms, and conditionally
    increment successful_calls. Called after every proxied invocation.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT avg_latency_ms, total_calls FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            return
        new_total = row["total_calls"] + 1
        new_avg = (row["avg_latency_ms"] * row["total_calls"] + latency_ms) / new_total
        conn.execute(
            """
            UPDATE agents
            SET total_calls      = ?,
                avg_latency_ms   = ?,
                successful_calls = successful_calls + ?
            WHERE agent_id = ?
            """,
            (new_total, round(new_avg, 2), 1 if success else 0, agent_id),
        )


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_agents(tag: str | None = None) -> list:
    """
    Return all agent listings, optionally filtered by tag.
    Tag filter uses exact match (tags are stored as a JSON array of strings).
    """
    with _conn() as conn:
        if tag:
            # Wrap in quotes to match exact tags, not substrings.
            # e.g. tag="fin" will NOT match "financial-research".
            rows = conn.execute(
                'SELECT * FROM agents WHERE tags LIKE ? ORDER BY created_at',
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
