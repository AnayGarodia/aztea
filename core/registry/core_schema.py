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
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np

from core import db as _db
from core import embeddings

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
    "call_latency_ring",
    "avg_latency_ms",
    "total_calls",
    "successful_calls",
    "tags",
    "input_schema",
    "output_schema",
    "output_verifier_url",
    "internal_only",
    "cacheable",
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
    "probation",
    "rejected",
}

_embeddings_cache_lock = threading.Lock()
_embeddings_cache_expires_at = 0.0
_embeddings_cache: dict[str, np.ndarray] = {}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _conn() -> _db.DbConnection:
    """Return a thread-local SQLite connection with WAL mode."""
    return _db.get_raw_connection(_resolved_db_path())


_AGENTS_DDL_TEMPLATE = f"""
    CREATE TABLE IF NOT EXISTS {{table_name}} (
        agent_id            TEXT PRIMARY KEY,
        owner_id            TEXT NOT NULL,
        name                TEXT NOT NULL UNIQUE,
        description         TEXT NOT NULL,
        endpoint_url        TEXT NOT NULL,
        healthcheck_url     TEXT,
        price_per_call_usd  REAL NOT NULL CHECK(price_per_call_usd >= 0),
        call_latency_ring   TEXT NOT NULL DEFAULT '[]',
        avg_latency_ms      REAL NOT NULL DEFAULT 0.0,
        total_calls         INTEGER NOT NULL DEFAULT 0,
        successful_calls    INTEGER NOT NULL DEFAULT 0,
        tags                TEXT NOT NULL DEFAULT '[]',
        input_schema        TEXT NOT NULL DEFAULT '{{{{}}}}',
        output_schema       TEXT NOT NULL DEFAULT '{{{{}}}}',
        output_verifier_url TEXT,
        output_examples     TEXT,
        verified            INTEGER NOT NULL DEFAULT 0,
        endpoint_health_status TEXT NOT NULL DEFAULT 'unknown' CHECK(endpoint_health_status IN ('unknown','healthy','degraded')),
        endpoint_consecutive_failures INTEGER NOT NULL DEFAULT 0,
        endpoint_last_checked_at TEXT,
        endpoint_last_error TEXT,
        internal_only       INTEGER NOT NULL DEFAULT 0,
        cacheable          INTEGER,
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
        pricing_config      TEXT,
        kind                TEXT NOT NULL DEFAULT 'self_hosted',
        pii_safe            INTEGER NOT NULL DEFAULT 0,
        outputs_not_stored  INTEGER NOT NULL DEFAULT 0,
        audit_logged        INTEGER NOT NULL DEFAULT 0,
        region_locked       TEXT
    )
"""


def _create_agents_table(conn: _db.DbConnection, table_name: str = "agents") -> None:
    """Side-effect: create the canonical agents table if absent. Single DDL statement."""
    conn.execute(_AGENTS_DDL_TEMPLATE.format(table_name=table_name))


def _create_agent_embeddings_table(conn: _db.DbConnection) -> None:
    conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_embeddings (
                agent_id     TEXT PRIMARY KEY REFERENCES agents(agent_id) ON DELETE CASCADE,
                embedding    BLOB NOT NULL,
                source_text  TEXT NOT NULL,
                embedded_at  TEXT NOT NULL
            )
        """)


def _ensure_agents_indexes(conn: _db.DbConnection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_name ON agents(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_created ON agents(created_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_embeddings_embedded_at ON agent_embeddings(embedded_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_model_provider ON agents(model_provider)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_price_cents ON agents(price_per_call_cents)"
    )


def _agents_table_exists(conn: _db.DbConnection) -> bool:
    if _db.IS_POSTGRES:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'agents'"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agents'"
        ).fetchone()
    return row is not None


def _agents_columns(conn: _db.DbConnection) -> dict:
    if _db.IS_POSTGRES:
        rows = conn.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'agents'"
        ).fetchall()
        return {row["name"]: {"name": row["name"], "pk": 0, "notnull": 1, "dflt_value": None} for row in rows}
    return {
        row["name"]: row for row in conn.execute("PRAGMA table_info(agents)").fetchall()
    }


def _has_unique_name_constraint(conn: _db.DbConnection) -> bool:
    # In Postgres mode we rely on the migration files to enforce constraints.
    if _db.IS_POSTGRES:
        return True
    for idx in conn.execute("PRAGMA index_list(agents)").fetchall():
        if idx["unique"] != 1:
            continue
        idx_name = str(idx["name"]).replace("'", "''")
        index_cols = conn.execute(f"PRAGMA index_info('{idx_name}')").fetchall()
        col_names = [row["name"] for row in index_cols]
        if col_names == ["name"]:
            return True
    return False


def _has_price_check_constraint(conn: _db.DbConnection) -> bool:
    # In Postgres mode constraints are managed by migration SQL files.
    if _db.IS_POSTGRES:
        return True
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agents'"
    ).fetchone()
    table_sql = row["sql"] if row and row["sql"] else ""
    return bool(_PRICE_CHECK_RE.search(table_sql))


_FLAG_NOT_NULL_COLS = (
    "owner_id", "name", "description", "endpoint_url", "price_per_call_usd",
    "last_decay_at", "created_at",
)
_DEFAULT_VALUE_CHECKS: tuple[tuple[str, frozenset[str]], ...] = (
    ("avg_latency_ms", frozenset({"0.0", "0", "0.00"})),
    ("total_calls", frozenset({"0"})),
    ("successful_calls", frozenset({"0"})),
    ("tags", frozenset({"'[]'", '"[]"', "[]"})),
    ("input_schema", frozenset({"'{}'", '"{}"', "{}"})),
    ("output_schema", frozenset({"'{}'", '"{}"', "{}"})),
    ("internal_only", frozenset({"0"})),
    ("status", frozenset({"'active'", '"active"', "active"})),
    ("review_status", frozenset({"'approved'", '"approved"', "approved"})),
    ("trust_decay_multiplier", frozenset({"1", "1.0", "1.00"})),
)


def _agents_schema_drifted(cols: dict, conn: _db.DbConnection) -> bool:
    """Pure-ish: True when the agents table has drifted from the canonical schema.

    Why: split out so the actual migration check stays at one assertion
    per invariant; SQLite ``PRAGMA table_info`` reports defaults as the
    raw SQL token (with quotes), so we accept several encodings.
    """
    if not _REQUIRED_COLUMNS.issubset(cols.keys()):
        return True
    if cols["agent_id"]["pk"] != 1:
        return True
    for col_name in _FLAG_NOT_NULL_COLS:
        if cols[col_name]["notnull"] != 1:
            return True
    for col_name, allowed in _DEFAULT_VALUE_CHECKS:
        if cols[col_name]["dflt_value"] not in allowed:
            return True
    return not (_has_unique_name_constraint(conn) and _has_price_check_constraint(conn))


def _needs_agents_migration(conn: _db.DbConnection) -> bool:
    """Side-effect: introspect the agents table; True when an in-place migration is required.

    Why: in Postgres mode, schema evolution is handled exclusively by SQL
    migration files; this Python check is SQLite-only.
    """
    if _db.IS_POSTGRES:
        return False
    cols = _agents_columns(conn)
    return _agents_schema_drifted(cols, conn)


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


def _build_embedding_source_text(
    name: str, description: str, tags: list[str], input_schema: dict
) -> str:
    clean_name = str(name or "").strip()
    clean_description = str(description or "").strip()
    clean_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    schema_text = json.dumps(
        input_schema if isinstance(input_schema, dict) else {}, sort_keys=True
    )
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
    conn: _db.DbConnection,
    agent_id: str,
    source_text: str,
    embedding_vector: list[float] | np.ndarray | None = None,
) -> bool:
    existing = conn.execute(
        "SELECT source_text FROM agent_embeddings WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    if existing and existing["source_text"] == source_text:
        return False

    vector = (
        embedding_vector
        if embedding_vector is not None
        else embeddings.embed_text(source_text)
    )
    conn.execute(
        """
        INSERT INTO agent_embeddings (agent_id, embedding, source_text, embedded_at)
        VALUES (%s, %s, %s, %s)
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


def _refresh_embedding_cache_if_expired(now: float) -> None:
    """Side-effect (mutating module globals): drop and refresh the cache window past TTL."""
    global _embeddings_cache_expires_at, _embeddings_cache
    if now >= _embeddings_cache_expires_at:
        _embeddings_cache = {}
        _embeddings_cache_expires_at = now + _EMBEDDING_CACHE_TTL_SECONDS


def _fetch_missing_embeddings(missing: list[str]) -> dict[str, np.ndarray]:
    """Side-effect: read missing embeddings from agent_embeddings; ``{}`` if input empty."""
    if not missing:
        return {}
    placeholders = ",".join("%s" for _ in missing)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT agent_id, embedding FROM agent_embeddings WHERE agent_id IN ({placeholders})",
            tuple(missing),
        ).fetchall()
    return {str(row["agent_id"]): _unpack_embedding(row["embedding"]) for row in rows}


def _load_embeddings_for_agents(agent_ids: set[str]) -> dict[str, np.ndarray]:
    """Side-effect: thread-safe cache + DB load of agent embeddings.

    Why: search ranking calls this on every query; the per-process cache
    keeps p50 fast while the TTL prevents stale embeddings on long-lived
    workers.
    """
    global _embeddings_cache_expires_at
    requested = {
        str(agent_id).strip() for agent_id in agent_ids if str(agent_id).strip()
    }
    if not requested:
        return {}
    with _embeddings_cache_lock:
        _refresh_embedding_cache_if_expired(time.monotonic())
        missing = sorted(requested.difference(_embeddings_cache.keys()))
    loaded = _fetch_missing_embeddings(missing)
    with _embeddings_cache_lock:
        if loaded:
            _embeddings_cache.update(loaded)
        _embeddings_cache_expires_at = max(
            _embeddings_cache_expires_at,
            time.monotonic() + _EMBEDDING_CACHE_TTL_SECONDS,
        )
        return {
            agent_id: _embeddings_cache[agent_id]
            for agent_id in requested
            if agent_id in _embeddings_cache
        }


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


def _resolve_legacy_agent_id(
    row: dict, name: str, legacy_rowid: int, used_agent_ids: set,
) -> str:
    """Pure: stable, dedup-safe agent_id for a legacy row.

    Why: legacy tables may have rows without ``agent_id``; deriving one
    from rowid+name+endpoint keeps the migration deterministic across re-runs.
    """
    raw_agent_id = str(row.get("agent_id") or "").strip()
    if not raw_agent_id:
        raw_agent_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"legacy-agent:{legacy_rowid}:{name}:{row.get('endpoint_url') or ''}",
        ))
    agent_id = raw_agent_id
    suffix = 2
    while agent_id in used_agent_ids:
        agent_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{raw_agent_id}:{legacy_rowid}:{suffix}",
        ))
        suffix += 1
    used_agent_ids.add(agent_id)
    return agent_id


def _normalize_legacy_int_flags(row: dict) -> dict[str, int | None]:
    """Pure: coerce 0/1 flag fields, preserving None for nullable ``cacheable``."""
    try:
        verified = 1 if int(row.get("verified") or 0) else 0
    except (TypeError, ValueError):
        verified = 0
    try:
        internal_only = 1 if int(row.get("internal_only") or 0) else 0
    except (TypeError, ValueError):
        internal_only = 0
    cacheable_raw = row.get("cacheable")
    try:
        cacheable = None if cacheable_raw is None else (1 if int(cacheable_raw) else 0)
    except (TypeError, ValueError):
        cacheable = None
    return {"verified": verified, "internal_only": internal_only, "cacheable": cacheable}


def _normalize_legacy_status(row: dict) -> dict[str, str]:
    """Pure: clamp status / review_status / endpoint_health_status to allowed values."""
    health = str(row.get("endpoint_health_status") or "unknown").strip().lower()
    status = str(row.get("status") or "active").strip().lower()
    review_status = str(row.get("review_status") or "approved").strip().lower()
    return {
        "endpoint_health_status": health if health in _VALID_HEALTH_STATUSES else "unknown",
        "status": status if status in _VALID_AGENT_STATUSES else "active",
        "review_status": review_status if review_status in REVIEW_STATUSES else "approved",
    }


def _normalize_legacy_examples(row: dict) -> str | None:
    """Pure: re-encode output_examples as a JSON string (or None) for safe storage."""
    try:
        raw_examples = row.get("output_examples")
        parsed_ex = json.loads(raw_examples) if raw_examples else None
        return json.dumps(parsed_ex) if isinstance(parsed_ex, list) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _normalize_legacy_call_counts(row: dict) -> tuple[int, int]:
    """Pure: ``(total_calls, successful_calls)`` with successful clamped to ≤ total."""
    total = _to_non_negative_int(row.get("total_calls"), default=0)
    successful = _to_non_negative_int(row.get("successful_calls"), default=0)
    return total, min(successful, total)


def _build_legacy_agent_tuple(
    *, agent_id: str, owner_id: str, name: str, description: str,
    endpoint_url: str, row: dict, flags: dict[str, int | None],
    statuses: dict[str, str], total_calls: int, successful_calls: int,
    trust_decay_multiplier: float,
) -> tuple:
    """Pure: assemble the column-order tuple expected by the agents INSERT statement."""
    return (
        agent_id,
        owner_id,
        name,
        description,
        endpoint_url,
        str(row.get("healthcheck_url") or "").strip() or None,
        _to_non_negative_float(row.get("price_per_call_usd"), default=0.0),
        str(row.get("call_latency_ring") or "[]").strip() or "[]",
        _to_non_negative_float(row.get("avg_latency_ms"), default=0.0),
        total_calls,
        successful_calls,
        _normalize_tags_json(row.get("tags")),
        _normalize_input_schema_json(row.get("input_schema")),
        _normalize_output_schema_json(row.get("output_schema")),
        str(row.get("output_verifier_url") or "").strip() or None,
        _normalize_legacy_examples(row),
        flags["verified"],
        statuses["endpoint_health_status"],
        _to_non_negative_int(row.get("endpoint_consecutive_failures"), default=0),
        str(row.get("endpoint_last_checked_at") or "").strip() or None,
        str(row.get("endpoint_last_error") or "").strip() or None,
        flags["internal_only"],
        flags["cacheable"],
        statuses["status"],
        statuses["review_status"],
        str(row.get("review_note") or "").strip() or None,
        str(row.get("reviewed_at") or "").strip() or None,
        str(row.get("reviewed_by") or "").strip() or None,
        trust_decay_multiplier,
        str(row.get("last_decay_at") or "").strip() or _CANONICAL_CREATED_AT,
        str(row.get("created_at") or "").strip() or _CANONICAL_CREATED_AT,
    )


def _normalize_legacy_agent_row(
    row: dict, used_agent_ids: set, used_names: set,
) -> tuple:
    """Pure: shape a legacy agent row into the canonical INSERT tuple.

    Why: the migration runs once per deploy; idempotent + dedup-aware
    behaviour lets us re-run safely if a deploy aborts mid-migration.
    """
    legacy_rowid = row.get("_legacy_rowid", 0)
    raw_name = str(row.get("name") or "").strip()
    name = _dedupe_name(raw_name or "Unnamed Agent", used_names)
    agent_id = _resolve_legacy_agent_id(row, name, legacy_rowid, used_agent_ids)
    flags = _normalize_legacy_int_flags(row)
    statuses = _normalize_legacy_status(row)
    total_calls, successful_calls = _normalize_legacy_call_counts(row)
    description = str(row.get("description") or "").strip() or "No description provided."
    owner_id = str(row.get("owner_id") or "").strip() or f"agent:{agent_id}"
    endpoint_url = (
        str(row.get("endpoint_url") or "").strip()
        or f"legacy://missing-endpoint/{agent_id}"
    )
    trust_decay_multiplier = _to_non_negative_float(
        row.get("trust_decay_multiplier"), default=1.0,
    ) or 1.0
    return _build_legacy_agent_tuple(
        agent_id=agent_id, owner_id=owner_id, name=name,
        description=description, endpoint_url=endpoint_url,
        row=row, flags=flags, statuses=statuses,
        total_calls=total_calls, successful_calls=successful_calls,
        trust_decay_multiplier=trust_decay_multiplier,
    )


def _migrate_agents_table(conn: _db.DbConnection) -> None:
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
                 call_latency_ring, avg_latency_ms, total_calls, successful_calls, tags, input_schema,
                 output_schema, output_verifier_url, output_examples, verified,
                 endpoint_health_status, endpoint_consecutive_failures, endpoint_last_checked_at, endpoint_last_error,
                 internal_only, cacheable, status, review_status, review_note, reviewed_at, reviewed_by,
                 trust_decay_multiplier, last_decay_at, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            normalized,
        )
    conn.execute("DROP TABLE agents")
    conn.execute("ALTER TABLE agents__canonical RENAME TO agents")


def _ensure_agent_identity_columns(conn: _db.DbConnection) -> None:
    """Add the cryptographic-identity columns to the agents table.

    Mirrors what migration 0015_agent_identity.sql does, so dev and test
    environments that bypass the migration runner still pick up the
    schema. Idempotent — duplicate-column errors are swallowed.
    """
    extras = [
        "ALTER TABLE agents ADD COLUMN did TEXT",
        "ALTER TABLE agents ADD COLUMN signing_public_key TEXT",
        "ALTER TABLE agents ADD COLUMN signing_private_key TEXT",
        "ALTER TABLE agents ADD COLUMN signing_alg TEXT NOT NULL DEFAULT 'ed25519'",
        "ALTER TABLE agents ADD COLUMN signing_keys_created_at TEXT",
    ]
    for ddl in extras:
        try:
            conn.execute(ddl)
        except _db.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_did "
        "ON agents(did) WHERE did IS NOT NULL"
    )


def _ensure_kind_column(conn: _db.DbConnection) -> None:
    """Add kind column to agents table if not present. Idempotent."""
    try:
        conn.execute(
            "ALTER TABLE agents ADD COLUMN kind TEXT NOT NULL DEFAULT 'self_hosted'"
        )
    except _db.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_kind ON agents(kind)")
    except _db.OperationalError:
        pass


def _ensure_privacy_tier_columns(conn: _db.DbConnection) -> None:
    extras = [
        "ALTER TABLE agents ADD COLUMN pii_safe INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE agents ADD COLUMN outputs_not_stored INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE agents ADD COLUMN audit_logged INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE agents ADD COLUMN region_locked TEXT",
    ]
    for ddl in extras:
        try:
            conn.execute(ddl)
        except _db.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _ensure_payout_curve_column(conn: _db.DbConnection) -> None:
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN payout_curve TEXT")
    except _db.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def _ensure_cacheable_column(conn: _db.DbConnection) -> None:
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN cacheable INTEGER")
    except _db.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def init_db() -> None:
    """Create or migrate the agents table to the canonical production schema.

    In PostgreSQL mode the schema comes entirely from migrations; all inline
    ALTER TABLE guards are SQLite-only and must not run against Postgres.
    """
    if _db.IS_POSTGRES:
        return
    with _conn() as conn:
        if not _agents_table_exists(conn):
            _create_agents_table(conn)
        elif _needs_agents_migration(conn):
            _migrate_agents_table(conn)
        _create_agent_embeddings_table(conn)
        _ensure_agents_indexes(conn)
        _ensure_agent_identity_columns(conn)
        _ensure_kind_column(conn)
        _ensure_privacy_tier_columns(conn)
        _ensure_payout_curve_column(conn)
        _ensure_cacheable_column(conn)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


_VALID_HEALTH_STATUSES = frozenset({"unknown", "healthy", "degraded"})
_VALID_AGENT_STATUSES = frozenset({"active", "suspended", "banned"})
_VALID_PRICING_MODELS = frozenset({"fixed", "per_unit", "tiered"})
_VALID_AGENT_KINDS = frozenset({"aztea_built", "community_skill", "self_hosted"})


def _parse_json_field(raw: Any, default_factory: Callable[[], Any], expected_type: type) -> Any:
    """Pure: decode a JSON-encoded scalar field; default-factory result on failure."""
    try:
        parsed = json.loads(raw or ("[]" if expected_type is list else "{}"))
    except (json.JSONDecodeError, TypeError):
        return default_factory()
    return parsed if isinstance(parsed, expected_type) else default_factory()


def _parse_json_blob_fields(d: dict) -> None:
    """Side-effect (mutating ``d``): decode tags / call_latency_ring / schema fields."""
    d["tags"] = _parse_json_field(d.get("tags"), list, list)
    d["call_latency_ring"] = _parse_json_field(d.get("call_latency_ring"), list, list)
    d["input_schema"] = _parse_json_field(d.get("input_schema"), dict, dict)
    d["output_schema"] = _parse_json_field(d.get("output_schema"), dict, dict)
    raw_examples = d.get("output_examples")
    try:
        parsed_examples = json.loads(raw_examples) if raw_examples else None
    except (json.JSONDecodeError, TypeError):
        parsed_examples = None
    d["output_examples"] = parsed_examples if isinstance(parsed_examples, list) else None


def _normalize_endpoint_health_fields(d: dict) -> None:
    """Side-effect (mutating ``d``): clamp endpoint health/error fields to known shapes."""
    health_status = str(d.get("endpoint_health_status") or "unknown").strip().lower()
    d["endpoint_health_status"] = (
        health_status if health_status in _VALID_HEALTH_STATUSES else "unknown"
    )
    d["endpoint_consecutive_failures"] = _to_non_negative_int(
        d.get("endpoint_consecutive_failures"), default=0,
    )
    d["endpoint_last_checked_at"] = (
        str(d.get("endpoint_last_checked_at") or "").strip() or None
    )
    d["endpoint_last_error"] = str(d.get("endpoint_last_error") or "").strip() or None


def _normalize_status_fields(d: dict) -> None:
    """Side-effect (mutating ``d``): clamp status / review_status / kind / cacheable fields."""
    cacheable_raw = d.get("cacheable")
    if cacheable_raw is None:
        d["cacheable"] = None
    else:
        try:
            d["cacheable"] = bool(int(cacheable_raw))
        except (TypeError, ValueError):
            d["cacheable"] = None
    status = str(d.get("status") or "active").strip().lower()
    d["status"] = status if status in _VALID_AGENT_STATUSES else "active"
    review_status = str(d.get("review_status") or "approved").strip().lower()
    d["review_status"] = review_status if review_status in REVIEW_STATUSES else "approved"
    raw_kind = str(d.get("kind") or "self_hosted").strip().lower()
    d["kind"] = raw_kind if raw_kind in _VALID_AGENT_KINDS else "self_hosted"


def _normalize_pricing_fields(d: dict) -> None:
    """Side-effect (mutating ``d``): decode pricing_model + pricing_config + payout_curve."""
    raw_pricing_model = str(d.get("pricing_model") or "fixed").strip().lower()
    d["pricing_model"] = (
        raw_pricing_model if raw_pricing_model in _VALID_PRICING_MODELS else "fixed"
    )
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
    raw_pc = d.get("payout_curve")
    if raw_pc:
        try:
            d["payout_curve"] = json.loads(raw_pc) if isinstance(raw_pc, str) else raw_pc
        except (ValueError, TypeError):
            d["payout_curve"] = None
    else:
        d["payout_curve"] = None


def _row_to_dict(row: dict) -> dict:
    """Pure-ish: project a raw DB row into the agent's canonical dict shape.

    Why: this is the boundary between SQL strings and the typed domain
    dict; every consumer assumes the shape is normalised, so all coercion
    happens exactly once here.
    """
    d = dict(row)
    _parse_json_blob_fields(d)
    d["healthcheck_url"] = str(d.get("healthcheck_url") or "").strip() or None
    d["output_verifier_url"] = d.get("output_verifier_url") or None
    d["verified"] = bool(int(d.get("verified") or 0))
    _normalize_endpoint_health_fields(d)
    d["internal_only"] = bool(int(d.get("internal_only") or 0))
    _normalize_status_fields(d)
    d["review_note"] = str(d.get("review_note") or "").strip() or None
    d["reviewed_at"] = str(d.get("reviewed_at") or "").strip() or None
    d["reviewed_by"] = str(d.get("reviewed_by") or "").strip() or None
    d["trust_decay_multiplier"] = (
        _to_non_negative_float(d.get("trust_decay_multiplier"), default=1.0) or 1.0
    )
    d["last_decay_at"] = str(d.get("last_decay_at") or _CANONICAL_CREATED_AT)
    d["model_provider"] = str(d.get("model_provider") or "").strip().lower() or None
    d["model_id"] = str(d.get("model_id") or "").strip() or None
    _normalize_pricing_fields(d)
    d["pii_safe"] = bool(int(d.get("pii_safe") or 0))
    d["outputs_not_stored"] = bool(int(d.get("outputs_not_stored") or 0))
    d["audit_logged"] = bool(int(d.get("audit_logged") or 0))
    d["region_locked"] = str(d.get("region_locked") or "").strip().lower() or None
    total = d["total_calls"]
    successful = d.pop("successful_calls")
    d["success_rate"] = round(successful / total, 4) if total > 0 else 1.0
    return d


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------
