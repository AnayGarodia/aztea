"""
vector_store.py — namespaced vector store with top-K cosine search.

# OWNS: public add/top_k/delete API, dispatch to backend, numpy-vectorised
#       brute-force top-K computation shared between SQLite and Postgres.
# NOT OWNS: schema (migration 0065), embeddings (core/embeddings.py),
#           caller-side metadata semantics (each namespace owner defines).
#
# INVARIANTS:
#   * Every vector is exactly EMBEDDING_DIM (384) floats.
#   * Vectors are stored as float32 numpy bytes for backend parity.
#   * Cosine similarity uses normalised vectors; scores in [-1, 1].
#   * Backend selection mirrors core/db.py (DATABASE_URL → Postgres/SQLite).
#   * Metadata is JSON-serialisable. Non-serialisable values raise at add-time.
#
# DECISIONS:
#   * v0 uses brute-force top-K on both backends. Scales fine to ~50k vectors
#     per namespace. pgvector promotion is detected (HAS_PGVECTOR) but the
#     fast-path query is a follow-up — the public API is unchanged so the swap
#     is transparent.
#   * top_k loads all candidate vectors into a single numpy matrix for one
#     vectorised dot-product. Beats per-row cosine by ~50x at N=1000.
#   * Filter predicate runs after retrieval (post-filter). Pre-filter would
#     require pushing JSON queries into SQL — out of v0 scope.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import numpy as np

from core import db as _db
from core.embeddings import EMBEDDING_DIM

_LOG = logging.getLogger(__name__)

# Hard cap so a single malformed call can't OOM a worker by requesting every
# vector. Reasoning agents asking for more than 200 hits are doing something
# wrong; the gate forces them to paginate or filter at a higher level.
_MAX_TOP_K = 200


@dataclass(frozen=True)
class VectorMatch:
    """One hit from top_k. score is cosine similarity in [-1, 1]."""

    namespace: str
    entry_id: str
    score: float
    metadata: dict[str, Any]


def _now_iso() -> str:
    """Pure: UTC ISO8601 'Z'-suffixed string for created_at columns."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_vector(vector: list[float] | np.ndarray) -> np.ndarray:
    """Pure: coerce to float32 ndarray, validate dimension. Raises ValueError."""
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    if arr.size != EMBEDDING_DIM:
        raise ValueError(
            f"vector dimension must be {EMBEDDING_DIM}, got {arr.size}"
        )
    if not np.isfinite(arr).all():
        raise ValueError("vector must contain only finite floats")
    return arr


def _normalize(arr: np.ndarray) -> np.ndarray:
    """Pure: return unit-length copy. Zero vectors return as-is.

    Why: precomputing norms once at add-time means top_k can do a pure
    dot product instead of a full cosine calculation per row.
    """
    norm = float(np.linalg.norm(arr))
    if norm <= 0.0:
        return arr.copy()
    return arr / norm


def _validate_namespace(namespace: str) -> None:
    """Pure: enforce namespace shape to keep table indexes selective."""
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("namespace must be a non-empty string")
    if len(namespace) > 128:
        raise ValueError(f"namespace must be <= 128 chars, got {len(namespace)}")


def _validate_entry_id(entry_id: str) -> None:
    """Pure: enforce entry_id shape so PRIMARY KEY behaves predictably."""
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise ValueError("entry_id must be a non-empty string")
    if len(entry_id) > 256:
        raise ValueError(f"entry_id must be <= 256 chars, got {len(entry_id)}")


def _validate_metadata(metadata: dict[str, Any]) -> str:
    """Serialise metadata to JSON; raises ValueError on bad input."""
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a dict")
    try:
        return json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"metadata must be JSON-serialisable: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add(
    namespace: str,
    entry_id: str,
    vector: list[float] | np.ndarray,
    metadata: dict[str, Any],
) -> None:
    """Insert or replace a vector under (namespace, entry_id).

    Why upsert and not insert-only: re-indexing the same git hunk should be
    idempotent. The hunk_id is the natural key for repo_hunks so a re-ingest
    of a previously-seen commit overwrites cleanly without bookkeeping.
    """
    _validate_namespace(namespace)
    _validate_entry_id(entry_id)
    arr = _validate_vector(vector)
    arr = _normalize(arr)
    metadata_json = _validate_metadata(metadata)
    blob = arr.tobytes()

    with _db.get_db_connection() as conn:
        with conn:
            _upsert_vector(conn, namespace, entry_id, blob, metadata_json)


def get(namespace: str, entry_id: str) -> VectorMatch | None:
    """Fetch a single entry by primary key. Returns None if absent."""
    _validate_namespace(namespace)
    _validate_entry_id(entry_id)
    with _db.get_db_connection() as conn:
        row = conn.execute(
            "SELECT namespace, entry_id, embedding, metadata FROM vector_entries "
            "WHERE namespace = %s AND entry_id = %s",
            (namespace, entry_id),
        ).fetchone()
    if row is None:
        return None
    return VectorMatch(
        namespace=row["namespace"],
        entry_id=row["entry_id"],
        score=1.0,  # Self-match by construction.
        metadata=json.loads(row["metadata"]),
    )


def delete(namespace: str, entry_id: str) -> bool:
    """Remove a single entry. Returns True if a row was deleted."""
    _validate_namespace(namespace)
    _validate_entry_id(entry_id)
    with _db.get_db_connection() as conn:
        with conn:
            cursor = conn.execute(
                "DELETE FROM vector_entries WHERE namespace = %s AND entry_id = %s",
                (namespace, entry_id),
            )
            return cursor.rowcount > 0


def delete_namespace(namespace: str) -> int:
    """Remove every entry under namespace. Returns the row count deleted."""
    _validate_namespace(namespace)
    with _db.get_db_connection() as conn:
        with conn:
            cursor = conn.execute(
                "DELETE FROM vector_entries WHERE namespace = %s",
                (namespace,),
            )
            return cursor.rowcount


def count(namespace: str) -> int:
    """Return how many entries exist under namespace."""
    _validate_namespace(namespace)
    with _db.get_db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM vector_entries WHERE namespace = %s",
            (namespace,),
        ).fetchone()
    return int(row["c"]) if row else 0


def top_k(
    namespace: str,
    query_vector: list[float] | np.ndarray,
    k: int = 10,
    filter_pred: Callable[[dict[str, Any]], bool] | None = None,
) -> list[VectorMatch]:
    """Return the top-k most-similar entries in namespace by cosine.

    Pre-filter is not supported in v0; filter_pred runs over the loaded
    candidate rows post-cosine. For namespaces with millions of entries
    this is the wrong shape — file an issue and we'll push filters into
    SQL or pgvector when needed.

    Why brute force: at v0 scale (≤50k per namespace) the numpy-vectorised
    dot-product against a unit-normalised matrix runs in a few ms. pgvector's
    ivfflat is faster at >100k but adds dependency surface we don't need yet.
    """
    _validate_namespace(namespace)
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if k > _MAX_TOP_K:
        raise ValueError(f"k must be <= {_MAX_TOP_K}, got {k}")
    query = _validate_vector(query_vector)
    query = _normalize(query)

    rows = _load_namespace_rows(namespace)
    if not rows:
        return []

    candidates = list(rows)
    matrix = np.frombuffer(
        b"".join(r["embedding"] for r in candidates),
        dtype=np.float32,
    ).reshape(len(candidates), EMBEDDING_DIM)
    scores = matrix @ query

    order = np.argsort(-scores)
    out: list[VectorMatch] = []
    for idx in order:
        if len(out) >= k:
            break
        row = candidates[idx]
        metadata = json.loads(row["metadata"])
        if filter_pred is not None and not filter_pred(metadata):
            continue
        out.append(
            VectorMatch(
                namespace=row["namespace"],
                entry_id=row["entry_id"],
                score=float(scores[idx]),
                metadata=metadata,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Backend I/O — kept here as the difference is one line of SQL; splitting into
# separate modules would be 90% repetition.
# ---------------------------------------------------------------------------


def _upsert_vector(
    conn: Any,
    namespace: str,
    entry_id: str,
    blob: bytes,
    metadata_json: str,
) -> None:
    """Backend-specific upsert. SQLite uses INSERT ... ON CONFLICT REPLACE;
    Postgres uses INSERT ... ON CONFLICT DO UPDATE. Both produce the same
    final row state.
    """
    created_at = _now_iso()
    if _db.IS_POSTGRES:
        conn.execute(
            "INSERT INTO vector_entries (namespace, entry_id, embedding, metadata, created_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (namespace, entry_id) DO UPDATE SET "
            "embedding = EXCLUDED.embedding, "
            "metadata  = EXCLUDED.metadata, "
            "created_at = EXCLUDED.created_at",
            (namespace, entry_id, blob, metadata_json, created_at),
        )
        return
    # SQLite: REPLACE atomically deletes the conflicting row and inserts.
    conn.execute(
        "INSERT OR REPLACE INTO vector_entries "
        "(namespace, entry_id, embedding, metadata, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (namespace, entry_id, blob, metadata_json, created_at),
    )


def _load_namespace_rows(namespace: str) -> list[dict[str, Any]]:
    """Load every (entry_id, embedding, metadata) row in namespace.

    Why load-all-then-rank: for v0 scales (≤50k rows × 384 floats × 4 bytes
    = 75 MB max) this fits comfortably in worker memory and avoids the
    per-row Python loop that dominates SQLite cursor iteration time.
    """
    with _db.get_db_connection() as conn:
        rows = conn.execute(
            "SELECT namespace, entry_id, embedding, metadata "
            "FROM vector_entries WHERE namespace = %s",
            (namespace,),
        ).fetchall()
    return rows


def add_batch(
    namespace: str,
    entries: Iterable[tuple[str, list[float] | np.ndarray, dict[str, Any]]],
) -> int:
    """Bulk upsert. Returns the number of rows processed.

    Why: ingest pipelines write thousands of vectors per repo; per-row
    INSERTs would dominate ingest time with transaction overhead.
    """
    _validate_namespace(namespace)
    count_in = 0
    with _db.get_db_connection() as conn:
        with conn:
            for entry_id, vector, metadata in entries:
                _validate_entry_id(entry_id)
                arr = _normalize(_validate_vector(vector))
                metadata_json = _validate_metadata(metadata)
                _upsert_vector(
                    conn,
                    namespace,
                    entry_id,
                    arr.tobytes(),
                    metadata_json,
                )
                count_in += 1
    return count_in


# ---------------------------------------------------------------------------
# Optional pgvector detection (cached at module load on Postgres only).
# ---------------------------------------------------------------------------
#
# HAS_PGVECTOR is informational for v0 — we don't yet have a fast-path query
# implementation. The detection runs once so the future fast-path can flip on
# without code changes. Detection is safe on Postgres clusters that deny the
# CREATE EXTENSION privilege (we swallow the error and stay on brute-force).
HAS_PGVECTOR: bool = False


def _detect_pgvector_once() -> None:
    """Side-effect: probe Postgres for the pgvector extension.

    Why: managed PG offerings vary widely on whether CREATE EXTENSION is
    permitted. We want the BLOB fallback to remain the default on any
    environment where the upgrade is impossible. Future work: add an
    `ensure_pgvector_schema()` that issues the ALTER COLUMN + ivfflat
    index when HAS_PGVECTOR is True.
    """
    global HAS_PGVECTOR
    if not _db.IS_POSTGRES:
        return
    try:
        with _db.get_db_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
        HAS_PGVECTOR = row is not None
    except Exception as exc:
        _LOG.debug("pgvector probe failed; staying on brute-force: %s", exc)
        HAS_PGVECTOR = False


_detect_pgvector_once()
