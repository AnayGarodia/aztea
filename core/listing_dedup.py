"""Duplicate detection for publish candidates.

# OWNS: two dup signals on a publish candidate —
#   1. exact-content fingerprint (the ONLY duplicate that hard-blocks), and
#   2. embedding cosine near-duplicate (advisory → probation, never blocks).
# NOT OWNS: the Jaccard first-pass clone scan (``core.listing_safety.scan_clone_against``)
#   or the embedding storage for ranking (``core.registry`` owns agent_embeddings).
# INVARIANTS:
#   - Only a full-body SHA-256 match is a BLOCK. Cosine similarity is WARN-level
#     evidence only (2026-06-03 decision D2 / H5 — subjective similarity must not
#     refuse a publish, it self-corrects in probation).
#   - The cosine pass reads vectors from the **agent_embeddings** table, NOT
#     core.vector_store (that backs the disjoint vector_entries store — querying it
#     would silently return nothing for every real publish; C1 of the review).
#   - When embeddings are disabled (AZTEA_DISABLE_EMBEDDINGS), the cosine pass is
#     skipped entirely — random vectors must never reach a similarity threshold.
# DECISIONS:
#   - Fingerprints strip frontmatter + normalise whitespace so a copy with only a
#     renamed title still collides, while shared SKILL.md frontmatter does not.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from core import db as _db
from core import embeddings
from core import feature_flags
from core.listing_safety import LEVEL_BLOCK, LEVEL_WARN, VerificationFinding
from core.registry.core_schema import _build_embedding_source_text, _resolved_db_path

_LOG = logging.getLogger(__name__)

# Re-export so tests' ``_close_module_conn`` helper can reach the thread-local.
_local = _db._local

CODE_DUPLICATE = "listing.duplicate"
CODE_NEAR_DUPLICATE = "listing.near_duplicate"

# Cosine at/above this is surfaced as a near-dup WARN. There is deliberately no
# cosine BLOCK threshold — exact copies are caught by the fingerprint instead.
_DUP_COSINE_WARN = feature_flags.flag_float("AZTEA_DUP_COSINE_WARN", default=0.85)

# Cap how many near-dup matches we surface — the buyer/reviewer only needs the
# closest few, not the whole long tail.
_MAX_NEAR_DUP_MATCHES = 5


@dataclass(frozen=True)
class DupMatch:
    agent_id: str
    name: str
    owner_id: str
    similarity: float  # 1.0 for an exact fingerprint match


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(_resolved_db_path())


# ---------------------------------------------------------------------------
# Exact-content fingerprint (the only dup hard-block)
# ---------------------------------------------------------------------------


def _strip_frontmatter(text: str) -> str:
    """Drop a leading ``---`` ... ``---`` YAML block (SKILL.md title/metadata)."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text
    lines = stripped.splitlines()
    # lines[0] is the opening '---'; find the closing fence.
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[idx + 1:])
    return text  # unterminated fence — treat as body


def normalize_body_for_fingerprint(body: str, kind: str) -> str:
    """Pure: collapse a body to a canonical form for exact-copy hashing.

    Strips SKILL.md frontmatter, drops blank lines, trims per-line whitespace,
    and lowercases. The goal: a copy whose only change is a renamed title or
    reflowed blank lines still hashes identically, while genuinely different
    bodies do not.
    """
    text = body or ""
    if kind == "skill_md":
        text = _strip_frontmatter(text)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines).lower()


def content_fingerprint(body: str, kind: str) -> str:
    """Pure: SHA-256 hex of the normalised body."""
    norm = normalize_body_for_fingerprint(body, kind)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def record_fingerprint(agent_id: str, body: str, kind: str) -> None:
    """Side-effect: upsert the fingerprint for an already-registered listing."""
    fp = content_fingerprint(body, kind)
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO listing_fingerprints (agent_id, fingerprint, created_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(agent_id) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                created_at = excluded.created_at
            """,
            (agent_id, fp, datetime.now(timezone.utc).isoformat()),
        )


def find_verbatim_copy(
    fingerprint: str,
    *,
    exclude_agent_id: str | None = None,
    exclude_owner_id: str | None = None,
) -> DupMatch | None:
    """Side-effect: return the first active listing sharing ``fingerprint``, if any.

    ``exclude_owner_id`` skips the publisher's own listings: the duplicate block
    is meant to stop copying *someone else's* agent, not to stop an owner from
    re-listing their own content (which the platform supports via name-collision
    suffixing).
    """
    sql = (
        "SELECT f.agent_id, a.name, a.owner_id "
        "FROM listing_fingerprints f JOIN agents a ON a.agent_id = f.agent_id "
        "WHERE f.fingerprint = %s AND a.status = 'active'"
    )
    params: list[object] = [fingerprint]
    if exclude_agent_id:
        sql += " AND f.agent_id != %s"
        params.append(exclude_agent_id)
    if exclude_owner_id:
        sql += " AND a.owner_id != %s"
        params.append(exclude_owner_id)
    sql += " LIMIT 1"
    with _conn() as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
    if not row:
        return None
    return DupMatch(
        agent_id=str(row["agent_id"]),
        name=str(row["name"]),
        owner_id=str(row["owner_id"]),
        similarity=1.0,
    )


def verbatim_finding(match: DupMatch) -> VerificationFinding:
    """Pure: the BLOCK finding for an exact-copy match.

    The caller-facing message deliberately does NOT name the matched listing: the
    only way to reach this block is to publish content byte-identical to *another
    owner's* listing (own content is owner-excluded), so naming it would hand a
    copier an existence/name oracle for other owners' (incl. internal) agents.
    Operators see the matched agent_id in the server log (see _fingerprint_block).
    """
    return VerificationFinding(
        code=CODE_DUPLICATE,
        level=LEVEL_BLOCK,
        message=(
            "This listing's content is byte-identical to a listing already on "
            "Aztea. We don't host exact copies. If you own the original, update "
            "it instead of re-publishing; otherwise add your own distinct logic."
        ),
        detail={},
    )


# ---------------------------------------------------------------------------
# Embedding cosine near-duplicate (advisory only)
# ---------------------------------------------------------------------------


def _unpack_embedding(blob: bytes | bytearray | memoryview) -> np.ndarray:
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size != embeddings.EMBEDDING_DIM:
        raise ValueError(
            f"embedding blob dimension must be {embeddings.EMBEDDING_DIM}, got {arr.size}"
        )
    return arr.astype(np.float32, copy=True)


def find_near_duplicates(
    name: str,
    description: str,
    tags: list[str],
    input_schema: dict | None,
    *,
    warn_at: float | None = None,
    exclude_agent_id: str | None = None,
    limit: int = _MAX_NEAR_DUP_MATCHES,
) -> list[DupMatch]:
    """Side-effect: cosine the candidate against every stored agent embedding.

    Returns matches at/above ``warn_at`` (advisory). Skips entirely when
    embeddings are disabled so random vectors never produce a false signal.
    Reads from ``agent_embeddings`` (the ranking store) — NOT core.vector_store.
    """
    if feature_flags.DISABLE_EMBEDDINGS:
        return []
    threshold = _DUP_COSINE_WARN if warn_at is None else warn_at
    source = _build_embedding_source_text(
        name, description, list(tags or []), input_schema if isinstance(input_schema, dict) else {},
    )
    try:
        candidate_vec = np.asarray(embeddings.embed_text(source), dtype=np.float32)
    except Exception:  # noqa: BLE001 — embedding failure must not break publishing
        _LOG.info("near-dup embed failed for candidate listing", exc_info=True)
        return []

    with _conn() as conn:
        rows = conn.execute(
            "SELECT e.agent_id, e.embedding, a.name, a.owner_id "
            "FROM agent_embeddings e JOIN agents a ON a.agent_id = e.agent_id "
            "WHERE a.status = 'active'"
        ).fetchall()

    matches: list[DupMatch] = []
    for row in rows:
        agent_id = str(row["agent_id"])
        if exclude_agent_id and agent_id == exclude_agent_id:
            continue
        try:
            other = _unpack_embedding(row["embedding"])
        except (ValueError, TypeError):
            # A wrong-dimension / corrupt embedding blob is skipped, but logged so
            # a systematically-bad column is observable rather than silently lost.
            _LOG.debug("skipping malformed embedding row for agent %s", agent_id)
            continue
        sim = embeddings.cosine(candidate_vec, other)
        if sim >= threshold:
            matches.append(DupMatch(
                agent_id=agent_id, name=str(row["name"]),
                owner_id=str(row["owner_id"]), similarity=sim,
            ))
    matches.sort(key=lambda m: m.similarity, reverse=True)
    return matches[:limit]


def near_duplicate_findings(matches: list[DupMatch]) -> list[VerificationFinding]:
    """Pure: one WARN finding summarising the closest near-duplicates (advisory)."""
    if not matches:
        return []
    top = matches[0]
    return [
        VerificationFinding(
            code=CODE_NEAR_DUPLICATE,
            level=LEVEL_WARN,
            message=(
                f"This listing is highly similar to '{top.name}' "
                f"(similarity {top.similarity:.2f}). Similar agents start in "
                "probation so buyers can compare track records before it ranks."
            ),
            detail={
                "matches": [
                    {"agent_id": m.agent_id, "name": m.name, "similarity": round(m.similarity, 4)}
                    for m in matches
                ],
            },
        )
    ]


__all__ = [
    "CODE_DUPLICATE",
    "CODE_NEAR_DUPLICATE",
    "DupMatch",
    "content_fingerprint",
    "find_near_duplicates",
    "find_verbatim_copy",
    "near_duplicate_findings",
    "normalize_body_for_fingerprint",
    "record_fingerprint",
    "verbatim_finding",
]
