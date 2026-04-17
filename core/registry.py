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
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import numpy as np

from core import embeddings
from core import db as _db

DB_PATH = _db.DB_PATH
_local = _db._local

_CANONICAL_CREATED_AT = "1970-01-01T00:00:00+00:00"
_PRICE_CHECK_RE = re.compile(
    r"check\s*\(\s*price_per_call_usd\s*>=\s*0(?:\.0+)?\s*\)",
    re.IGNORECASE,
)
_REQUIRED_COLUMNS = {
    "agent_id",
    "owner_id",
    "output_examples",
    "verified",
    "name",
    "description",
    "endpoint_url",
    "price_per_call_usd",
    "avg_latency_ms",
    "total_calls",
    "successful_calls",
    "tags",
    "input_schema",
    "output_schema",
    "output_verifier_url",
    "internal_only",
    "status",
    "trust_decay_multiplier",
    "last_decay_at",
    "created_at",
}
_EMBEDDING_CACHE_TTL_SECONDS = 60
SEMANTIC_SIMILARITY_WEIGHT = 0.5
TRUST_SCORE_WEIGHT = 0.3
INVERSE_PRICE_WEIGHT = 0.2
_TRUST_PERCENT_SCALE = 100.0
_QUERY_STOP_WORDS = {
    "a",
    "an",
    "the",
    "to",
    "for",
    "of",
    "and",
    "or",
    "in",
    "on",
    "with",
    "i",
    "need",
}

_embeddings_cache_lock = threading.Lock()
_embeddings_cache_expires_at = 0.0
_embeddings_cache: dict[str, np.ndarray] = {}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL mode."""
    return _db.get_raw_connection(DB_PATH)


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
                output_schema       TEXT NOT NULL DEFAULT '{{}}',
                output_verifier_url TEXT,
                output_examples     TEXT,
                verified            INTEGER NOT NULL DEFAULT 0,
                internal_only       INTEGER NOT NULL DEFAULT 0,
                status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','banned')),
                trust_decay_multiplier REAL NOT NULL DEFAULT 1.0,
                last_decay_at       TEXT NOT NULL DEFAULT '{_CANONICAL_CREATED_AT}',
                created_at          TEXT NOT NULL
            )
        """)


def _create_agent_embeddings_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_embeddings (
                agent_id     TEXT PRIMARY KEY REFERENCES agents(agent_id) ON DELETE CASCADE,
                embedding    BLOB NOT NULL,
                source_text  TEXT NOT NULL,
                embedded_at  TEXT NOT NULL
            )
        """)


def _ensure_agents_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_name ON agents(name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_created ON agents(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_embeddings_embedded_at ON agent_embeddings(embedded_at DESC)"
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
    if cols["output_schema"]["dflt_value"] not in {"'{}'", '"{}"', "{}"}:
        return True
    if cols["internal_only"]["dflt_value"] != "0":
        return True
    if cols["status"]["dflt_value"] not in {"'active'", '"active"', "active"}:
        return True
    if cols["trust_decay_multiplier"]["dflt_value"] not in {"1", "1.0", "1.00"}:
        return True
    if cols["last_decay_at"]["notnull"] != 1:
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


def _normalize_output_schema_json(raw_schema) -> str:
    return _normalize_input_schema_json(raw_schema)


def _parse_tags(raw_tags) -> list[str]:
    if isinstance(raw_tags, str):
        try:
            parsed = json.loads(raw_tags)
        except json.JSONDecodeError:
            parsed = []
    elif isinstance(raw_tags, list):
        parsed = raw_tags
    else:
        parsed = []
    return [str(tag).strip() for tag in parsed if str(tag).strip()]


def _parse_input_schema(raw_schema) -> dict:
    if isinstance(raw_schema, str):
        try:
            parsed = json.loads(raw_schema)
        except json.JSONDecodeError:
            parsed = {}
    elif isinstance(raw_schema, dict):
        parsed = raw_schema
    else:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_output_schema(raw_schema) -> dict:
    return _parse_input_schema(raw_schema)


def _build_embedding_source_text(name: str, description: str, tags: list[str], input_schema: dict) -> str:
    clean_name = str(name or "").strip()
    clean_description = str(description or "").strip()
    clean_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    schema_text = json.dumps(input_schema if isinstance(input_schema, dict) else {}, sort_keys=True)
    return f"{clean_name}. {clean_description}. Tags: {', '.join(clean_tags)}. Input: {schema_text}"


def _embedding_source_from_agent(agent: dict) -> str:
    return _build_embedding_source_text(
        str(agent.get("name") or ""),
        str(agent.get("description") or ""),
        _parse_tags(agent.get("tags")),
        _parse_input_schema(agent.get("input_schema")),
    )


def _pack_embedding(vector: list[float] | np.ndarray) -> bytes:
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    if arr.size != embeddings.EMBEDDING_DIM:
        raise ValueError(
            f"embedding vector must have dimension {embeddings.EMBEDDING_DIM}, got {arr.size}"
        )
    return arr.tobytes()


def _unpack_embedding(blob: bytes | bytearray | memoryview) -> np.ndarray:
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size != embeddings.EMBEDDING_DIM:
        raise ValueError(
            f"embedding blob dimension must be {embeddings.EMBEDDING_DIM}, got {arr.size}"
        )
    return arr.astype(np.float32, copy=True)


def _upsert_agent_embedding_row(
    conn: sqlite3.Connection,
    agent_id: str,
    source_text: str,
    embedding_vector: list[float] | np.ndarray | None = None,
) -> bool:
    existing = conn.execute(
        "SELECT source_text FROM agent_embeddings WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if existing and existing["source_text"] == source_text:
        return False

    vector = embedding_vector if embedding_vector is not None else embeddings.embed_text(source_text)
    conn.execute(
        """
        INSERT INTO agent_embeddings (agent_id, embedding, source_text, embedded_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(agent_id)
        DO UPDATE SET
            embedding = excluded.embedding,
            source_text = excluded.source_text,
            embedded_at = excluded.embedded_at
        """,
        (
            agent_id,
            _pack_embedding(vector),
            source_text,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return True


def _invalidate_embeddings_cache() -> None:
    global _embeddings_cache_expires_at, _embeddings_cache
    with _embeddings_cache_lock:
        _embeddings_cache = {}
        _embeddings_cache_expires_at = 0.0


def _load_embeddings_for_agents(agent_ids: set[str]) -> dict[str, np.ndarray]:
    global _embeddings_cache_expires_at, _embeddings_cache
    requested = {str(agent_id).strip() for agent_id in agent_ids if str(agent_id).strip()}
    if not requested:
        return {}

    now = time.monotonic()
    with _embeddings_cache_lock:
        if now >= _embeddings_cache_expires_at:
            _embeddings_cache = {}
            _embeddings_cache_expires_at = now + _EMBEDDING_CACHE_TTL_SECONDS
        cached = {agent_id: _embeddings_cache[agent_id] for agent_id in requested if agent_id in _embeddings_cache}
        missing = sorted(requested.difference(cached.keys()))

    loaded: dict[str, np.ndarray] = {}
    if missing:
        placeholders = ",".join("?" for _ in missing)
        with _conn() as conn:
            rows = conn.execute(
                f"SELECT agent_id, embedding FROM agent_embeddings WHERE agent_id IN ({placeholders})",
                tuple(missing),
            ).fetchall()
        for row in rows:
            loaded[str(row["agent_id"])] = _unpack_embedding(row["embedding"])

    with _embeddings_cache_lock:
        if loaded:
            _embeddings_cache.update(loaded)
        _embeddings_cache_expires_at = max(
            _embeddings_cache_expires_at,
            time.monotonic() + _EMBEDDING_CACHE_TTL_SECONDS,
        )
        return {agent_id: _embeddings_cache[agent_id] for agent_id in requested if agent_id in _embeddings_cache}


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
    output_schema = _normalize_output_schema_json(row.get("output_schema"))
    output_verifier_url = str(row.get("output_verifier_url") or "").strip() or None
    try:
        raw_examples = row.get("output_examples")
        parsed_ex = json.loads(raw_examples) if raw_examples else None
        output_examples = json.dumps(parsed_ex) if isinstance(parsed_ex, list) else None
    except (json.JSONDecodeError, TypeError):
        output_examples = None
    try:
        verified = 1 if int(row.get("verified") or 0) else 0
    except (TypeError, ValueError):
        verified = 0
    try:
        internal_only = 1 if int(row.get("internal_only") or 0) else 0
    except (TypeError, ValueError):
        internal_only = 0
    status = str(row.get("status") or "active").strip().lower()
    if status not in {"active", "suspended", "banned"}:
        status = "active"
    trust_decay_multiplier = _to_non_negative_float(row.get("trust_decay_multiplier"), default=1.0)
    if trust_decay_multiplier <= 0:
        trust_decay_multiplier = 1.0
    last_decay_at = str(row.get("last_decay_at") or "").strip() or _CANONICAL_CREATED_AT
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
        output_schema,
        output_verifier_url,
        output_examples,
        verified,
        internal_only,
        status,
        trust_decay_multiplier,
        last_decay_at,
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
                 avg_latency_ms, total_calls, successful_calls, tags, input_schema,
                 output_schema, output_verifier_url, output_examples, verified,
                 internal_only, status, trust_decay_multiplier, last_decay_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        _create_agent_embeddings_table(conn)
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
    try:
        parsed_output_schema = json.loads(d.get("output_schema") or "{}")
        d["output_schema"] = parsed_output_schema if isinstance(parsed_output_schema, dict) else {}
    except (json.JSONDecodeError, TypeError):
        d["output_schema"] = {}
    d["output_verifier_url"] = (d.get("output_verifier_url") or None)
    try:
        raw_examples = d.get("output_examples")
        parsed_examples = json.loads(raw_examples) if raw_examples else None
        d["output_examples"] = parsed_examples if isinstance(parsed_examples, list) else None
    except (json.JSONDecodeError, TypeError):
        d["output_examples"] = None
    d["verified"] = bool(int(d.get("verified") or 0))
    d["internal_only"] = bool(int(d.get("internal_only") or 0))
    status = str(d.get("status") or "active").strip().lower()
    d["status"] = status if status in {"active", "suspended", "banned"} else "active"
    d["trust_decay_multiplier"] = _to_non_negative_float(d.get("trust_decay_multiplier"), default=1.0) or 1.0
    d["last_decay_at"] = str(d.get("last_decay_at") or _CANONICAL_CREATED_AT)

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
    output_schema: dict | None = None,
    output_verifier_url: str | None = None,
    output_examples: list | None = None,
    verified: bool = False,
    internal_only: bool = False,
    status: str = "active",
    trust_decay_multiplier: float = 1.0,
    owner_id: str | None = None,
    embed_listing: bool = True,
) -> str:
    """
    Insert a new agent listing. Returns the agent_id.
    Pass agent_id explicitly for deterministic IDs (e.g. self-registration).
    By default this also writes an embedding row in the same request.
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
    normalized_tags = _parse_tags(tags)
    normalized_schema = _parse_input_schema(input_schema)
    normalized_output_schema = _parse_output_schema(output_schema)
    schema_json = json.dumps(normalized_schema, sort_keys=True)
    output_schema_json = json.dumps(normalized_output_schema, sort_keys=True)
    tags_json = json.dumps(normalized_tags)
    normalized_verifier_url = str(output_verifier_url or "").strip() or None
    if isinstance(output_examples, list):
        normalized_examples: str | None = json.dumps(
            [ex for ex in output_examples if isinstance(ex, dict)]
        ) or None
    else:
        normalized_examples = None
    normalized_verified = 1 if verified else 0
    normalized_status = str(status or "active").strip().lower()
    if normalized_status not in {"active", "suspended", "banned"}:
        raise ValueError("status must be one of: active, suspended, banned.")
    normalized_decay_multiplier = _to_non_negative_float(trust_decay_multiplier, default=1.0)
    if normalized_decay_multiplier <= 0:
        normalized_decay_multiplier = 1.0
    internal_only_int = 1 if internal_only else 0
    source_text = ""
    embedding_vector: list[float] | None = None
    if embed_listing:
        source_text = _build_embedding_source_text(name, description, normalized_tags, normalized_schema)
        embedding_vector = embeddings.embed_text(source_text)
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agents
                (agent_id, owner_id, name, description, endpoint_url,
                 price_per_call_usd, tags, input_schema, output_schema, output_verifier_url,
                 output_examples, verified, internal_only, status, trust_decay_multiplier, last_decay_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                aid,
                normalized_owner_id,
                name,
                description,
                endpoint_url,
                price,
                tags_json,
                schema_json,
                output_schema_json,
                normalized_verifier_url,
                normalized_examples,
                normalized_verified,
                internal_only_int,
                normalized_status,
                normalized_decay_multiplier,
                created_at,
                created_at,
            ),
        )
        if embed_listing and embedding_vector is not None:
            _upsert_agent_embedding_row(
                conn,
                agent_id=aid,
                source_text=source_text,
                embedding_vector=embedding_vector,
            )
    if embed_listing:
        _invalidate_embeddings_cache()
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

def get_agents(tag: str | None = None, include_internal: bool = False, include_banned: bool = False) -> list:
    """
    Return all agent listings, optionally filtered by tag.
    Tag matching uses exact JSON-array membership to avoid substring false-positives.
    """
    with _conn() as conn:
        where_clauses: list[str] = []
        params: list[Any] = []
        if not include_internal:
            where_clauses.append("internal_only = 0")
        if not include_banned:
            where_clauses.append("status NOT IN ('banned', 'suspended')")
        if tag:
            where_clauses.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        rows = conn.execute(
            f"SELECT * FROM agents {where_sql} ORDER BY created_at",
            tuple(params),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_agent_status(agent_id: str, status: str) -> dict | None:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"active", "suspended", "banned"}:
        raise ValueError("status must be one of: active, suspended, banned.")
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET status = ? WHERE agent_id = ?",
            (normalized_status, agent_id),
        )
    return get_agent(agent_id)


def touch_agent_decay(agent_id: str, at_iso: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET last_decay_at = ? WHERE agent_id = ?",
            (str(at_iso or _CANONICAL_CREATED_AT), agent_id),
        )


def set_agent_decay_multiplier(agent_id: str, multiplier: float, at_iso: str) -> None:
    parsed = _to_non_negative_float(multiplier, default=1.0)
    parsed = max(0.0, min(1.0, parsed))
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET trust_decay_multiplier = ?, last_decay_at = ? WHERE agent_id = ?",
            (parsed, str(at_iso or _CANONICAL_CREATED_AT), agent_id),
        )


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


def sync_agent_embedding(agent_id: str) -> bool:
    """
    Re-embed one agent if its current source_text has changed.
    This is the helper future update paths should call after mutating
    name/description/tags/input_schema.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Agent '{agent_id}' not found.")

        agent = _row_to_dict(row)
        source_text = _embedding_source_from_agent(agent)
        changed = _upsert_agent_embedding_row(conn, agent_id, source_text)
    if changed:
        _invalidate_embeddings_cache()
    return changed


def backfill_missing_embeddings(limit: int | None = None) -> dict[str, int]:
    """
    Embed existing agents that do not yet have rows in agent_embeddings.
    Safe to run repeatedly (idempotent).
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1 when provided.")

    with _conn() as conn:
        query = """
            SELECT a.*
            FROM agents AS a
            LEFT JOIN agent_embeddings AS e ON e.agent_id = a.agent_id
            WHERE e.agent_id IS NULL
            ORDER BY a.created_at, a.agent_id
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        rows = conn.execute(query, params).fetchall()

    to_embed: list[tuple[str, str, list[float]]] = []
    for row in rows:
        agent = _row_to_dict(row)
        source_text = _embedding_source_from_agent(agent)
        to_embed.append(
            (
                str(agent["agent_id"]),
                source_text,
                embeddings.embed_text(source_text),
            )
        )

    embedded = 0
    if to_embed:
        with _conn() as conn:
            for agent_id, source_text, vector in to_embed:
                if _upsert_agent_embedding_row(
                    conn,
                    agent_id=agent_id,
                    source_text=source_text,
                    embedding_vector=vector,
                ):
                    embedded += 1
    if embedded > 0:
        _invalidate_embeddings_cache()

    return {"scanned": len(rows), "embedded": embedded}


def _normalize_trust_score(value: float | int | None) -> float:
    trust = _to_non_negative_float(value, default=0.0)
    if trust > 1.0:
        trust = trust / _TRUST_PERCENT_SCALE
    return max(0.0, min(1.0, trust))


def _normalize_min_trust(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError("min_trust must be between 0.0 and 1.0.")
    if not math.isfinite(parsed):
        raise ValueError("min_trust must be between 0.0 and 1.0.")
    if parsed < 0.0:
        raise ValueError("min_trust must be between 0.0 and 1.0.")
    if parsed > 1.0 and parsed <= _TRUST_PERCENT_SCALE:
        parsed = parsed / _TRUST_PERCENT_SCALE
    if parsed < 0.0 or parsed > 1.0:
        raise ValueError("min_trust must be between 0.0 and 1.0.")
    return parsed


def _price_usd_to_cents(value: float | int | str | None) -> int:
    try:
        amount = Decimal(str(value))
    except Exception:
        return 0
    if amount < 0:
        return 0
    cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if amount > 0 and cents == 0:
        return 1
    return cents


def _required_input_fields_set(required_input_fields: list[str] | None) -> set[str]:
    if not required_input_fields:
        return set()
    fields: set[str] = set()
    for field in required_input_fields:
        value = str(field).strip()
        if not value:
            raise ValueError("required_input_fields entries must be non-empty strings.")
        fields.add(value)
    return fields


def _input_schema_field_names(schema: dict) -> set[str]:
    if not isinstance(schema, dict):
        return set()

    properties = schema.get("properties")
    if isinstance(properties, dict):
        return {str(name) for name in properties.keys()}

    fields = schema.get("fields")
    if isinstance(fields, list):
        names: set[str] = set()
        for field in fields:
            if isinstance(field, dict):
                candidate = str(field.get("name") or "").strip()
                if candidate:
                    names.add(candidate)
        return names
    return set()


def _input_schema_caller_trust_min(schema: dict) -> float | None:
    if not isinstance(schema, dict):
        return None
    candidate = schema.get("min_caller_trust")
    if candidate is None and isinstance(schema.get("metadata"), dict):
        candidate = schema["metadata"].get("min_caller_trust")
    if candidate is None:
        return None
    try:
        value = float(candidate)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if value > 1.0 and value <= 100.0:
        value = value / 100.0
    if value < 0.0 or value > 1.0:
        return None
    return value


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[a-z0-9-]+", query.lower())
    return [term for term in terms if term not in _QUERY_STOP_WORDS]


def _matched_phrase(query: str, haystack: str) -> str | None:
    terms = _query_terms(query)
    if not terms:
        return None

    lowered = haystack.lower()
    for width in (3, 2):
        if len(terms) < width:
            continue
        for idx in range(0, len(terms) - width + 1):
            phrase = " ".join(terms[idx: idx + width])
            if phrase in lowered:
                return phrase

    for term in terms:
        if len(term) >= 4 and term in lowered:
            return term
    return None


def _match_reasons(
    agent: dict,
    query: str,
    trust: float,
    required_fields: set[str],
    supported_fields: set[str],
    caller_trust: float | None,
    caller_trust_min: float | None,
) -> list[str]:
    reasons: list[str] = []
    haystack = " ".join(
        [
            str(agent.get("name") or ""),
            str(agent.get("description") or ""),
            " ".join(_parse_tags(agent.get("tags"))),
        ]
    )
    phrase = _matched_phrase(query, haystack)
    if phrase:
        reasons.append(f"matched '{phrase}' in description")
    if required_fields:
        if len(required_fields) == 1:
            field = sorted(required_fields)[0]
            reasons.append(f"supports {field} input field")
        else:
            ordered = ", ".join(sorted(supported_fields))
            reasons.append(f"supports input fields: {ordered}")
    reasons.append(f"trust {trust:.2f}")
    if caller_trust is not None and caller_trust_min is not None:
        reasons.append(f"caller trust {caller_trust:.2f} meets minimum {caller_trust_min:.2f}")
    return reasons


def search_agents(
    query: str,
    limit: int = 10,
    min_trust: float = 0.0,
    max_price_cents: int | None = None,
    required_input_fields: list[str] | None = None,
    caller_trust: float | None = None,
) -> list[dict]:
    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ValueError("query must be a non-empty string.")
    if limit < 1:
        raise ValueError("limit must be >= 1.")
    if max_price_cents is not None and max_price_cents < 0:
        raise ValueError("max_price_cents must be >= 0 when provided.")

    trust_floor = _normalize_min_trust(min_trust)
    normalized_caller_trust = None
    if caller_trust is not None:
        normalized_caller_trust = _normalize_min_trust(caller_trust)
    required_fields = _required_input_fields_set(required_input_fields)
    query_vector = np.asarray(embeddings.embed_text(normalized_query), dtype=np.float32)
    agents = get_agents_with_reputation()
    vectors_by_agent = _load_embeddings_for_agents(
        {
            str(agent.get("agent_id") or "").strip()
            for agent in agents
            if str(agent.get("agent_id") or "").strip()
        }
    )

    missing_embeddings: list[tuple[str, str, list[float]]] = []
    candidates: list[dict] = []

    for agent in agents:
        agent_id = str(agent.get("agent_id") or "").strip()
        if not agent_id:
            continue

        price_cents = _price_usd_to_cents(agent.get("price_per_call_usd"))
        if max_price_cents is not None and price_cents > max_price_cents:
            continue

        schema = _parse_input_schema(agent.get("input_schema"))
        supported_fields = _input_schema_field_names(schema)
        caller_trust_min = _input_schema_caller_trust_min(schema)
        if required_fields and not required_fields.issubset(supported_fields):
            continue
        if (
            normalized_caller_trust is not None
            and caller_trust_min is not None
            and normalized_caller_trust < caller_trust_min
        ):
            continue

        trust = _normalize_trust_score(agent.get("trust_score"))
        if trust < trust_floor:
            continue

        vector = vectors_by_agent.get(agent_id)
        if vector is None:
            source_text = _embedding_source_from_agent(agent)
            vector_list = embeddings.embed_text(source_text)
            vector = np.asarray(vector_list, dtype=np.float32)
            vectors_by_agent[agent_id] = vector
            missing_embeddings.append((agent_id, source_text, vector_list))

        similarity = float(embeddings.cosine(query_vector, vector))
        semantic_similarity = max(0.0, min(1.0, similarity))
        candidates.append(
            {
                "agent": agent,
                "similarity": semantic_similarity,
                "trust": trust,
                "price_cents": price_cents,
                "supported_fields": supported_fields,
                "caller_trust_min": caller_trust_min,
            }
        )

    if missing_embeddings:
        with _conn() as conn:
            changed = False
            for agent_id, source_text, vector_list in missing_embeddings:
                if _upsert_agent_embedding_row(
                    conn,
                    agent_id=agent_id,
                    source_text=source_text,
                    embedding_vector=vector_list,
                ):
                    changed = True
        if changed:
            _invalidate_embeddings_cache()

    if not candidates:
        return []

    price_values = [c["price_cents"] for c in candidates]
    min_price = min(price_values)
    max_price = max(price_values)

    for candidate in candidates:
        if max_price == min_price:
            inverse_price = 1.0
        else:
            normalized_price = (candidate["price_cents"] - min_price) / (max_price - min_price)
            inverse_price = 1.0 - normalized_price

        blended_score = (
            SEMANTIC_SIMILARITY_WEIGHT * candidate["similarity"]
            + TRUST_SCORE_WEIGHT * candidate["trust"]
            + INVERSE_PRICE_WEIGHT * inverse_price
        )
        candidate["blended_score"] = blended_score
        candidate["match_reasons"] = _match_reasons(
            candidate["agent"],
            normalized_query,
            candidate["trust"],
            required_fields,
            candidate["supported_fields"],
            normalized_caller_trust,
            candidate["caller_trust_min"],
        )

    ranked = sorted(
        candidates,
        key=lambda item: (
            item["blended_score"],
            item["similarity"],
            item["trust"],
            -item["price_cents"],
        ),
        reverse=True,
    )

    return [
        {
            "agent": item["agent"],
            "similarity": round(item["similarity"], 6),
            "trust": round(item["trust"], 6),
            "blended_score": round(item["blended_score"], 6),
            "match_reasons": item["match_reasons"],
        }
        for item in ranked[:limit]
    ]


def get_agents_with_reputation(tag: str | None = None) -> list:
    """Return listings enriched with trust/reputation fields for ranking."""
    from core import reputation

    return reputation.enrich_agent_records(get_agents(tag=tag))


def get_agent_with_reputation(agent_id: str) -> dict | None:
    """Return one enriched listing by agent_id, or None if missing."""
    from core import reputation

    agent = get_agent(agent_id)
    return reputation.enrich_agent_record(agent) if agent else None
