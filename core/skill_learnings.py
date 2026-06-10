"""
skill_learnings.py — Storage + rendering for hosted-skill "learnings memory".

# OWNS: the skill_learnings table (CRUD), the proposal de-dup / pending cap, the
#   owner-scoped status transition, and rendering the active-learnings block that
#   core/skill_executor.py injects at execution time.
# NOT OWNS: the distiller (core/observability.py decides WHAT to propose), the
#   injection wiring (core/skill_executor.py), the HTTP routes (server/routes/
#   skill_learnings.py), the feature flag (core/feature_flags.py).
# INVARIANTS:
#   - The stored hosted_skills.system_prompt is never touched here. A learning's
#     only effect is the injected block; reversal is a status flip to 'archived'.
#   - Status transitions are append-only in spirit: no hard DELETE in the normal
#     flow. archive_learnings_for_skill flips to 'archived'.
#   - set_learning_status is owner-scoped (WHERE owner_id) + rowcount-guarded so a
#     non-owner can never mutate another owner's learnings.
#   - The injected block is bounded (_MAX_ACTIVE_LEARNINGS, _MAX_LEARNING_CHARS)
#     so an unbounded learning list can never bloat the prompt.
# DECISIONS: skill_id is stored as plain TEXT with no DB-level FK (hosted_skills
#   already cascades from agents; a RESTRICT-ing FK would block that cascade on
#   Postgres). Orphan cleanup is app-side via archive_learnings_for_skill.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core import db as _db
from core.registry.core_schema import _resolved_db_path

# Status lifecycle. 'proposed' -> owner accepts -> 'active' (injected) | owner
# rejects -> 'archived'. archive_learnings_for_skill also lands in 'archived'.
STATUS_PROPOSED = "proposed"
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
_DECIDABLE_STATUSES = frozenset({STATUS_ACTIVE, STATUS_ARCHIVED})

# Valid provenance tags for where a learning's signal came from.
_VALID_SOURCE_SIGNALS = frozenset({"rating", "dispute", "example"})

# Injection bounds. At most this many active learnings are rendered, each
# truncated to this many characters, so the injected block stays small and
# predictable regardless of how many learnings accumulate.
_MAX_ACTIVE_LEARNINGS = 12
_MAX_LEARNING_CHARS = 240

# Delimiters for the injected block. Kept distinctive so it reads as DATA, not
# instructions, when nested inside the executor's hardened system scaffolding.
_BLOCK_HEADER = "Operator learnings (apply when relevant):"


@dataclass(frozen=True)
class ProposedLearning:
    """One distilled corrective bullet awaiting owner review.

    Pure value object produced by the distiller and handed to
    propose_learnings. confidence is the distiller's self-rating (0..1);
    source_job_ids is provenance for the owner reviewing the proposal.
    """

    text: str
    source_signal: str
    confidence: float | None = None
    source_job_ids: list[str] = field(default_factory=list)


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(_resolved_db_path())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    """Lowercased, whitespace-collapsed form used only for de-dup comparison."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _row_to_dict(row: dict | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    raw_ids = out.get("source_job_ids")
    try:
        out["source_job_ids"] = json.loads(raw_ids) if raw_ids else []
    except (TypeError, json.JSONDecodeError):
        out["source_job_ids"] = []
    return out


def list_learnings(
    skill_id: str, status: str | None = None
) -> list[dict[str, Any]]:
    """Return a skill's learnings, newest first; optionally filtered by status."""
    with _conn() as conn:
        if status is None:
            rows = conn.execute(
                "SELECT * FROM skill_learnings WHERE skill_id = %s "
                "ORDER BY created_at DESC",
                (skill_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM skill_learnings WHERE skill_id = %s AND status = %s "
                "ORDER BY created_at DESC",
                (skill_id, status),
            ).fetchall()
    return [d for d in (_row_to_dict(r) for r in rows) if d is not None]


def get_learning(learning_id: str) -> dict[str, Any] | None:
    """Single learning by id (used by the decision route to resolve ownership)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM skill_learnings WHERE learning_id = %s",
            (learning_id,),
        ).fetchone()
    return _row_to_dict(row)


def count_pending(skill_id: str) -> int:
    """Number of 'proposed' (un-reviewed) learnings for a skill."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM skill_learnings "
            "WHERE skill_id = %s AND status = %s",
            (skill_id, STATUS_PROPOSED),
        ).fetchone()
    return int((row or {}).get("n", 0))


def _existing_normalized_texts(skill_id: str) -> set[str]:
    """Normalized text of every proposed/active learning — the de-dup set.

    Archived learnings are intentionally excluded: a previously-rejected bullet
    that the distiller surfaces again is allowed back as a fresh proposal.
    """
    rows = list_learnings(skill_id, status=STATUS_PROPOSED)
    rows += list_learnings(skill_id, status=STATUS_ACTIVE)
    return {_normalize(r.get("text", "")) for r in rows}


def propose_learnings(
    skill_id: str,
    agent_id: str,
    owner_id: str,
    learnings: list[ProposedLearning],
    *,
    max_pending: int,
) -> int:
    """Insert distilled learnings as 'proposed'; return the count actually written.

    De-dups against existing proposed/active text (normalized) and refuses to
    add anything once the skill already has >= max_pending un-reviewed proposals,
    so the owner's review queue cannot grow without bound. Side-effecting.
    """
    pending_now = count_pending(skill_id)
    if pending_now >= max_pending:
        return 0

    seen = _existing_normalized_texts(skill_id)
    now = _now_iso()
    to_write: list[tuple] = []
    for item in learnings:
        text = (item.text or "").strip()
        if not text:
            continue
        norm = _normalize(text)
        if norm in seen:
            continue
        signal = item.source_signal if item.source_signal in _VALID_SOURCE_SIGNALS else "example"
        seen.add(norm)
        to_write.append(
            (
                str(uuid.uuid4()), skill_id, agent_id, owner_id, text,
                STATUS_PROPOSED, signal,
                json.dumps(list(item.source_job_ids or [])),
                item.confidence, now,
            )
        )
        # Never let one distill pass blow past the pending cap.
        if pending_now + len(to_write) >= max_pending:
            break

    if not to_write:
        return 0
    with _conn() as conn:
        conn.executemany(
            """
            INSERT INTO skill_learnings
                (learning_id, skill_id, agent_id, owner_id, text, status,
                 source_signal, source_job_ids, confidence, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            to_write,
        )
    return len(to_write)


def set_learning_status(learning_id: str, owner_id: str, status: str) -> bool:
    """Owner-scoped status transition (accept -> active, reject -> archived).

    Returns True iff a row owned by ``owner_id`` was updated. The WHERE clause
    on owner_id plus the rowcount guard make a cross-owner mutation impossible.
    """
    if status not in _DECIDABLE_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_DECIDABLE_STATUSES)}; got {status!r}"
        )
    with _conn() as conn:
        result = conn.execute(
            "UPDATE skill_learnings SET status = %s, decided_at = %s, decided_by = %s "
            "WHERE learning_id = %s AND owner_id = %s",
            (status, _now_iso(), owner_id, learning_id, owner_id),
        )
    return result.rowcount > 0


def archive_learnings_for_skill(skill_id: str) -> int:
    """Flip all of a skill's non-archived learnings to 'archived'.

    Called inside the DELETE /skills transaction so deleting a skill leaves no
    dangling active learnings (app-level cleanup; no DB cascade). Returns the
    number of rows archived.
    """
    with _conn() as conn:
        result = conn.execute(
            "UPDATE skill_learnings SET status = %s WHERE skill_id = %s "
            "AND status != %s",
            (STATUS_ARCHIVED, skill_id, STATUS_ARCHIVED),
        )
    return int(result.rowcount or 0)


def active_learnings_block(skill_id: str) -> str | None:
    """Render the active-learnings block for injection, or None if there are none.

    Bounded by _MAX_ACTIVE_LEARNINGS (oldest-first, stable) and per-bullet by
    _MAX_LEARNING_CHARS. The output is plain text the executor wraps as DATA.
    """
    rows = list_learnings(skill_id, status=STATUS_ACTIVE)
    if not rows:
        return None
    # Inject the most-recent N active learnings — list_learnings is newest-first,
    # so the head is the newest. (Taking the head before sorting ensures a freshly
    # accepted learning is never starved by older ones once a skill exceeds the
    # cap.) Then render oldest-first within that window for a stable block.
    rows = sorted(rows[:_MAX_ACTIVE_LEARNINGS], key=lambda r: r.get("created_at", ""))
    bullets = []
    for r in rows:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        if len(text) > _MAX_LEARNING_CHARS:
            text = text[: _MAX_LEARNING_CHARS - 1].rstrip() + "…"
        bullets.append(f"- {text}")
    if not bullets:
        return None
    return _BLOCK_HEADER + "\n" + "\n".join(bullets)
