"""
registry.py — SQLite-backed agent registry for the agentmarket platform.

Production notes:
  - WAL mode enabled for concurrent read performance under load.
  - Thread-local connections (SQLite is not thread-safe across connections;
    each thread gets its own handle).
  - Indexes on name and created_at for fast discovery lookups.
  - input_schema stored as JSON; describes the fields a caller must supply.
"""

import json
import math
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "registry.db")
_local = threading.local()
_CANONICAL_CREATED_AT = "1970-01-01T00:00:00+00:00"
_PRICE_CHECK_RE = re.compile(
    r"check\s*\(\s*price_per_call_usd\s*>=\s*0(?:\.0+)?\s*\)",
    re.IGNORECASE,
)
_REQUIRED_COLUMNS = {
    "agent_id",
    "owner_id",
    "name",
    "description",
    "endpoint_url",
    "price_per_call_usd",
    "avg_latency_ms",
    "total_calls",
    "successful_calls",
    "tags",
    "input_schema",
    "created_at",
}


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


def _create_agents_table(conn: sqlite3.Connection, table_name: str = "agents") -> None:
    conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                agent_id            TEXT PRIMARY KEY,
                owner_id            TEXT NOT NULL,
                name                TEXT NOT NULL UNIQUE,
                description         TEXT NOT NULL,
                endpoint_url        TEXT NOT NULL,
                price_per_call_usd  REAL NOT NULL CHECK(price_per_call_usd >= 0),
                avg_latency_ms      REAL NOT NULL DEFAULT 0.0,
                total_calls         INTEGER NOT NULL DEFAULT 0,
                successful_calls    INTEGER NOT NULL DEFAULT 0,
                tags                TEXT NOT NULL DEFAULT '[]',
                input_schema        TEXT NOT NULL DEFAULT '{{}}',
                created_at          TEXT NOT NULL
            )
        """)


def _ensure_agents_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_name ON agents(name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_created ON agents(created_at)"
    )


def _agents_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agents'"
    ).fetchone()
    return row is not None


def _agents_columns(conn: sqlite3.Connection) -> dict:
    return {
        row["name"]: row
        for row in conn.execute("PRAGMA table_info(agents)").fetchall()
    }


def _has_unique_name_constraint(conn: sqlite3.Connection) -> bool:
    for idx in conn.execute("PRAGMA index_list(agents)").fetchall():
        if idx["unique"] != 1:
            continue
        idx_name = str(idx["name"]).replace("'", "''")
        index_cols = conn.execute(
            f"PRAGMA index_info('{idx_name}')"
        ).fetchall()
        col_names = [row["name"] for row in index_cols]
        if col_names == ["name"]:
            return True
    return False


def _has_price_check_constraint(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agents'"
    ).fetchone()
    table_sql = row["sql"] if row and row["sql"] else ""
    return bool(_PRICE_CHECK_RE.search(table_sql))


def _needs_agents_migration(conn: sqlite3.Connection) -> bool:
    cols = _agents_columns(conn)
    if not _REQUIRED_COLUMNS.issubset(cols.keys()):
        return True
    if cols["agent_id"]["pk"] != 1:
        return True
    if cols["owner_id"]["notnull"] != 1:
        return True
    if cols["name"]["notnull"] != 1 or cols["description"]["notnull"] != 1:
        return True
    if cols["endpoint_url"]["notnull"] != 1 or cols["price_per_call_usd"]["notnull"] != 1:
        return True
    if cols["avg_latency_ms"]["dflt_value"] not in {"0.0", "0", "0.00"}:
        return True
    if cols["total_calls"]["dflt_value"] != "0":
        return True
    if cols["successful_calls"]["dflt_value"] != "0":
        return True
    if cols["tags"]["dflt_value"] not in {"'[]'", '"[]"', "[]"}:
        return True
    if cols["input_schema"]["dflt_value"] not in {"'{}'", '"{}"', "{}"}:
        return True
    if cols["created_at"]["notnull"] != 1:
        return True
    if not _has_unique_name_constraint(conn):
        return True
    if not _has_price_check_constraint(conn):
        return True
    return False


def _to_non_negative_float(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed < 0:
        return default
    return parsed


def _to_non_negative_int(value, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _normalize_tags_json(raw_tags) -> str:
    if raw_tags is None:
        return "[]"
    parsed = raw_tags
    if isinstance(raw_tags, str):
        stripped = raw_tags.strip()
        if not stripped:
            return "[]"
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = [stripped]
    if not isinstance(parsed, list):
        parsed = []
    normalized = [str(tag).strip() for tag in parsed if str(tag).strip()]
    return json.dumps(normalized)


def _normalize_input_schema_json(raw_schema) -> str:
    if raw_schema is None:
        return "{}"
    parsed = raw_schema
    if isinstance(raw_schema, str):
        stripped = raw_schema.strip()
        if not stripped:
            return "{}"
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return "{}"
    if not isinstance(parsed, dict):
        parsed = {}
    return json.dumps(parsed)


def _dedupe_name(base_name: str, used_names: set) -> str:
    if base_name not in used_names:
        used_names.add(base_name)
        return base_name
    n = 2
    while True:
        candidate = f"{base_name} ({n})"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        n += 1


def _normalize_legacy_agent_row(row: dict, used_agent_ids: set, used_names: set) -> tuple:
    legacy_rowid = row.get("_legacy_rowid", 0)
    raw_name = str(row.get("name") or "").strip()
    name = _dedupe_name(raw_name or "Unnamed Agent", used_names)

    raw_agent_id = str(row.get("agent_id") or "").strip()
    if not raw_agent_id:
        raw_agent_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"legacy-agent:{legacy_rowid}:{name}:{row.get('endpoint_url') or ''}",
            )
        )

    agent_id = raw_agent_id
    suffix = 2
    while agent_id in used_agent_ids:
        agent_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{raw_agent_id}:{legacy_rowid}:{suffix}",
            )
        )
        suffix += 1
    used_agent_ids.add(agent_id)

    description = str(row.get("description") or "").strip() or "No description provided."
    owner_id = str(row.get("owner_id") or "").strip() or f"agent:{agent_id}"
    endpoint_url = str(row.get("endpoint_url") or "").strip() or f"legacy://missing-endpoint/{agent_id}"
    price_per_call_usd = _to_non_negative_float(row.get("price_per_call_usd"), default=0.0)
    avg_latency_ms = _to_non_negative_float(row.get("avg_latency_ms"), default=0.0)
    total_calls = _to_non_negative_int(row.get("total_calls"), default=0)
    successful_calls = _to_non_negative_int(row.get("successful_calls"), default=0)
    if successful_calls > total_calls:
        successful_calls = total_calls
    tags = _normalize_tags_json(row.get("tags"))
    input_schema = _normalize_input_schema_json(row.get("input_schema"))
    created_at = str(row.get("created_at") or "").strip() or _CANONICAL_CREATED_AT

    return (
        agent_id,
        owner_id,
        name,
        description,
        endpoint_url,
        price_per_call_usd,
        avg_latency_ms,
        total_calls,
        successful_calls,
        tags,
        input_schema,
        created_at,
    )


def _migrate_agents_table(conn: sqlite3.Connection) -> None:
    columns = _agents_columns(conn)
    order_by = "created_at, rowid" if "created_at" in columns else "rowid"
    legacy_rows = conn.execute(
        f"SELECT rowid AS _legacy_rowid, * FROM agents ORDER BY {order_by}"
    ).fetchall()

    conn.execute("DROP TABLE IF EXISTS agents__canonical")
    _create_agents_table(conn, table_name="agents__canonical")

    used_agent_ids = set()
    used_names = set()
    for row in legacy_rows:
        normalized = _normalize_legacy_agent_row(dict(row), used_agent_ids, used_names)
        conn.execute(
            """
            INSERT INTO agents__canonical
                (agent_id, owner_id, name, description, endpoint_url, price_per_call_usd,
                 avg_latency_ms, total_calls, successful_calls, tags, input_schema, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalized,
        )
    conn.execute("DROP TABLE agents")
    conn.execute("ALTER TABLE agents__canonical RENAME TO agents")


def init_db() -> None:
    """Create or migrate the agents table to the canonical production schema."""
    with _conn() as conn:
        if not _agents_table_exists(conn):
            _create_agents_table(conn)
        elif _needs_agents_migration(conn):
            _migrate_agents_table(conn)
        _ensure_agents_indexes(conn)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        parsed_tags = json.loads(d.get("tags") or "[]")
        d["tags"] = parsed_tags if isinstance(parsed_tags, list) else []
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []

    try:
        parsed_schema = json.loads(d.get("input_schema") or "{}")
        d["input_schema"] = parsed_schema if isinstance(parsed_schema, dict) else {}
    except (json.JSONDecodeError, TypeError):
        d["input_schema"] = {}

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
    owner_id: str | None = None,
) -> str:
    """
    Insert a new agent listing. Returns the agent_id.
    Pass agent_id explicitly for deterministic IDs (e.g. self-registration).
    Raises sqlite3.IntegrityError if agent_id already exists.
    """
    try:
        price = float(price_per_call_usd)
    except (TypeError, ValueError):
        raise ValueError("price_per_call_usd must be a non-negative number.")
    if not math.isfinite(price):
        raise ValueError("price_per_call_usd must be a finite non-negative number.")
    if price < 0:
        raise ValueError("price_per_call_usd must be non-negative.")

    aid = agent_id or str(uuid.uuid4())
    normalized_owner_id = (owner_id or f"agent:{aid}").strip()
    if not normalized_owner_id:
        raise ValueError("owner_id must be a non-empty string.")
    created_at = datetime.now(timezone.utc).isoformat()
    schema_json = json.dumps(input_schema or {})
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agents
                (agent_id, owner_id, name, description, endpoint_url,
                 price_per_call_usd, tags, input_schema, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (aid, normalized_owner_id, name, description, endpoint_url,
              price, json.dumps(tags), schema_json, created_at),
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


def get_agents_by_owner(owner_id: str) -> list:
    """Return all agents owned by the given owner_id."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agents WHERE owner_id = ? ORDER BY created_at",
            (owner_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def agent_exists_by_name(name: str) -> bool:
    """Return True if any agent with this name is already registered."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM agents WHERE name = ?", (name,)
        ).fetchone()
    return row is not None


def get_agents_with_reputation(tag: str | None = None) -> list:
    """Return listings enriched with trust/reputation fields for ranking."""
    from core import reputation

    return reputation.enrich_agent_records(get_agents(tag=tag))


def get_agent_with_reputation(agent_id: str) -> dict | None:
    """Return one enriched listing by agent_id, or None if missing."""
    from core import reputation

    agent = get_agent(agent_id)
    return reputation.enrich_agent_record(agent) if agent else None
