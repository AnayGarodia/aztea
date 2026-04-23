"""Registry persistence: schema, connection helpers, row serialisation.

This is the lower half of the ``core.registry`` package. The higher-level
operations (writes, semantic search, reputation enrichment, endpoint health
telemetry) live in ``core.registry.agents_ops``.

Responsibilities:

- SQLite schema creation for the ``agents`` table and its supporting indexes,
  plus migration-safe defaults for columns added post-launch (verified badge,
  model provider / id, review status, endpoint health, trust cache).
- Thread-local connection helpers (``_conn``, ``_local``) — tests monkeypatch
  ``DB_PATH`` between runs, so these are intentionally module-level.
- Row → dict projection (``_row_to_dict``) that handles JSON columns
  (``input_schema``, ``output_schema``, ``output_examples``, ``tags``) and
  normalises optional columns to ``None`` for clients that pre-date a
  schema addition.
- Shared constants (status enums, default rank-by values) used by both the
  write path and the server shards.

Production notes:

- WAL mode is enabled for concurrent read performance under marketplace load.
- Indexes on ``name`` and ``created_at`` keep discovery lookups fast.
- ``input_schema`` / ``output_schema`` / ``output_examples`` are stored as JSON
  and validated before insert via ``core.models.AgentRegisterRequest``.
"""

import json
import logging
import math
import re
import sqlite3
import sys
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


def _resolved_db_path() -> str:
    """Prefer ``core.registry.DB_PATH`` for isolated tests."""
    pkg = sys.modules.get("core.registry")
    if pkg is not None:
        c = getattr(pkg, "DB_PATH", None)
        if isinstance(c, str) and c:
            return c
    return DB_PATH


_logger = logging.getLogger(__name__)

HEALTH_SUSPENSION_THRESHOLD = 5

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
    "endpoint_health_status",
    "endpoint_consecutive_failures",
    "endpoint_last_checked_at",
    "endpoint_last_error",
    "name",
    "description",
    "endpoint_url",
    "healthcheck_url",
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
    "review_status",
    "review_note",
    "reviewed_at",
    "reviewed_by",
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
REVIEW_STATUSES = {
    "approved",
    "pending_review",
    "rejected",
}

_embeddings_cache_lock = threading.Lock()
_embeddings_cache_expires_at = 0.0
_embeddings_cache: dict[str, np.ndarray] = {}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL mode."""
    return _db.get_raw_connection(_resolved_db_path())


def _create_agents_table(conn: sqlite3.Connection, table_name: str = "agents") -> None:
    conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                agent_id            TEXT PRIMARY KEY,
                owner_id            TEXT NOT NULL,
                name                TEXT NOT NULL UNIQUE,
                description         TEXT NOT NULL,
                endpoint_url        TEXT NOT NULL,
                healthcheck_url     TEXT,
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
                endpoint_health_status TEXT NOT NULL DEFAULT 'unknown' CHECK(endpoint_health_status IN ('unknown','healthy','degraded')),
                endpoint_consecutive_failures INTEGER NOT NULL DEFAULT 0,
                endpoint_last_checked_at TEXT,
                endpoint_last_error TEXT,
                internal_only       INTEGER NOT NULL DEFAULT 0,
                status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','banned')),
                suspension_reason   TEXT,
                review_status       TEXT NOT NULL DEFAULT 'approved',
                review_note         TEXT,
                reviewed_at         TEXT,
                reviewed_by         TEXT,
                trust_decay_multiplier REAL NOT NULL DEFAULT 1.0,
                last_decay_at       TEXT NOT NULL DEFAULT '{_CANONICAL_CREATED_AT}',
                created_at          TEXT NOT NULL,
                model_provider      TEXT,
                model_id            TEXT,
                price_per_call_cents INTEGER,
                pricing_model       TEXT NOT NULL DEFAULT 'fixed',
                pricing_config      TEXT
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_model_provider ON agents(model_provider)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_price_cents ON agents(price_per_call_cents)"
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
    if cols["review_status"]["dflt_value"] not in {"'approved'", '"approved"', "approved"}:
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
    healthcheck_url = str(row.get("healthcheck_url") or "").strip() or None
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
    endpoint_health_status = str(row.get("endpoint_health_status") or "unknown").strip().lower()
    if endpoint_health_status not in {"unknown", "healthy", "degraded"}:
        endpoint_health_status = "unknown"
    endpoint_consecutive_failures = _to_non_negative_int(
        row.get("endpoint_consecutive_failures"),
        default=0,
    )
    endpoint_last_checked_at = str(row.get("endpoint_last_checked_at") or "").strip() or None
    endpoint_last_error = str(row.get("endpoint_last_error") or "").strip() or None
    try:
        internal_only = 1 if int(row.get("internal_only") or 0) else 0
    except (TypeError, ValueError):
        internal_only = 0
    status = str(row.get("status") or "active").strip().lower()
    if status not in {"active", "suspended", "banned"}:
        status = "active"
    review_status = str(row.get("review_status") or "approved").strip().lower()
    if review_status not in REVIEW_STATUSES:
        review_status = "approved"
    review_note = str(row.get("review_note") or "").strip() or None
    reviewed_at = str(row.get("reviewed_at") or "").strip() or None
    reviewed_by = str(row.get("reviewed_by") or "").strip() or None
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
        healthcheck_url,
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
        endpoint_health_status,
        endpoint_consecutive_failures,
        endpoint_last_checked_at,
        endpoint_last_error,
        internal_only,
        status,
        review_status,
        review_note,
        reviewed_at,
        reviewed_by,
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
                (agent_id, owner_id, name, description, endpoint_url, healthcheck_url, price_per_call_usd,
                 avg_latency_ms, total_calls, successful_calls, tags, input_schema,
                 output_schema, output_verifier_url, output_examples, verified,
                 endpoint_health_status, endpoint_consecutive_failures, endpoint_last_checked_at, endpoint_last_error,
                 internal_only, status, review_status, review_note, reviewed_at, reviewed_by,
                 trust_decay_multiplier, last_decay_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    d["healthcheck_url"] = str(d.get("healthcheck_url") or "").strip() or None
    d["output_verifier_url"] = (d.get("output_verifier_url") or None)
    try:
        raw_examples = d.get("output_examples")
        parsed_examples = json.loads(raw_examples) if raw_examples else None
        d["output_examples"] = parsed_examples if isinstance(parsed_examples, list) else None
    except (json.JSONDecodeError, TypeError):
        d["output_examples"] = None
    d["verified"] = bool(int(d.get("verified") or 0))
    endpoint_health_status = str(d.get("endpoint_health_status") or "unknown").strip().lower()
    if endpoint_health_status not in {"unknown", "healthy", "degraded"}:
        endpoint_health_status = "unknown"
    d["endpoint_health_status"] = endpoint_health_status
    d["endpoint_consecutive_failures"] = _to_non_negative_int(
        d.get("endpoint_consecutive_failures"),
        default=0,
    )
    d["endpoint_last_checked_at"] = str(d.get("endpoint_last_checked_at") or "").strip() or None
    d["endpoint_last_error"] = str(d.get("endpoint_last_error") or "").strip() or None
    d["internal_only"] = bool(int(d.get("internal_only") or 0))
    status = str(d.get("status") or "active").strip().lower()
    d["status"] = status if status in {"active", "suspended", "banned"} else "active"
    review_status = str(d.get("review_status") or "approved").strip().lower()
    d["review_status"] = review_status if review_status in REVIEW_STATUSES else "approved"
    d["review_note"] = str(d.get("review_note") or "").strip() or None
    d["reviewed_at"] = str(d.get("reviewed_at") or "").strip() or None
    d["reviewed_by"] = str(d.get("reviewed_by") or "").strip() or None
    d["trust_decay_multiplier"] = _to_non_negative_float(d.get("trust_decay_multiplier"), default=1.0) or 1.0
    d["last_decay_at"] = str(d.get("last_decay_at") or _CANONICAL_CREATED_AT)
    d["model_provider"] = str(d.get("model_provider") or "").strip().lower() or None
    d["model_id"] = str(d.get("model_id") or "").strip() or None
    raw_pricing_model = str(d.get("pricing_model") or "fixed").strip().lower()
    if raw_pricing_model not in {"fixed", "per_unit", "tiered"}:
        raw_pricing_model = "fixed"
    d["pricing_model"] = raw_pricing_model
    raw_pricing_config = d.get("pricing_config")
    parsed_pricing_config: dict | None = None
    if isinstance(raw_pricing_config, str) and raw_pricing_config.strip():
        try:
            candidate = json.loads(raw_pricing_config)
            parsed_pricing_config = candidate if isinstance(candidate, dict) else None
        except (json.JSONDecodeError, TypeError):
            parsed_pricing_config = None
    elif isinstance(raw_pricing_config, dict):
        parsed_pricing_config = raw_pricing_config
    d["pricing_config"] = parsed_pricing_config

    total = d["total_calls"]
    successful = d.pop("successful_calls")
    d["success_rate"] = round(successful / total, 4) if total > 0 else 1.0
    return d


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------
