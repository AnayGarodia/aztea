"""
trust_trend.py — directional quality signal for marketplace agents.

# OWNS: computing whether an agent's recent buyer ratings are improving, flat,
#   or declining vs the immediately-preceding window, from job_quality_ratings.
# NOT OWNS: the trust score itself (core/reputation.py), ranking (auto_hire /
#   search) — those import this for a small, bounded directional nudge.
# INVARIANTS: pure read-only. Returns 'unknown' (never raises) when there is
#   not enough rating history to judge a direction.
# DECISIONS: lives in its own module so core/reputation.py (already large) does
#   not grow; the batch entry point mirrors reputation's N-bounded map pattern.
"""

from __future__ import annotations

from typing import Iterable

from core import db as _db

TREND_IMPROVING = "improving"
TREND_FLAT = "flat"
TREND_DECLINING = "declining"
TREND_UNKNOWN = "unknown"

# Compare the most-recent _TREND_WINDOW ratings against the _TREND_WINDOW before
# them. Need at least _TREND_MIN_EACH on each side to call a direction, so a
# couple of stray ratings can't flip the label. _TREND_DELTA is in rating points
# (1-5 scale): the average must move by at least this much to be non-flat.
_TREND_WINDOW = 10
_TREND_MIN_EACH = 3
_TREND_DELTA = 0.3


def _trend_from_ratings(newest_first: list[int]) -> str:
    """Pure: classify a single agent's rating history (newest first)."""
    recent = newest_first[:_TREND_WINDOW]
    prior = newest_first[_TREND_WINDOW : _TREND_WINDOW * 2]
    if len(recent) < _TREND_MIN_EACH or len(prior) < _TREND_MIN_EACH:
        return TREND_UNKNOWN
    delta = (sum(recent) / len(recent)) - (sum(prior) / len(prior))
    if delta >= _TREND_DELTA:
        return TREND_IMPROVING
    if delta <= -_TREND_DELTA:
        return TREND_DECLINING
    return TREND_FLAT


def compute_trust_trends(agent_ids: Iterable[str]) -> dict[str, str]:
    """Batch: map each agent_id to its trend label. Never raises.

    One query over job_quality_ratings for all requested agents, then a pure
    per-agent classification. Agents with too little history map to 'unknown'.
    """
    ids = [str(a) for a in agent_ids if a]
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    # newest first so the per-agent slice [:N] is the most recent window.
    query = (
        "SELECT agent_id, rating FROM job_quality_ratings "
        f"WHERE agent_id IN ({placeholders}) ORDER BY created_at DESC"
    )
    by_agent: dict[str, list[int]] = {a: [] for a in ids}
    # Lazy import: resolves to the test DB under isolated_db (which patches
    # core.registry.DB_PATH) while breaking the reputation -> trust_trend ->
    # registry import cycle that a module-level import would create.
    from core.registry.core_schema import _resolved_db_path

    try:
        with _db.get_raw_connection(_resolved_db_path()) as conn:
            rows = conn.execute(query, tuple(ids)).fetchall()
    except _db.OperationalError:
        # Table missing pre-migration, or transient — degrade to unknown.
        return {a: TREND_UNKNOWN for a in ids}
    for row in rows:
        rec = dict(row)
        aid = str(rec.get("agent_id") or "")
        rating = rec.get("rating")
        if aid in by_agent and isinstance(rating, int):
            by_agent[aid].append(rating)
    return {aid: _trend_from_ratings(ratings) for aid, ratings in by_agent.items()}


def compute_trust_trend(agent_id: str) -> str:
    """Single-agent trend label (wraps the batch path)."""
    return compute_trust_trends([agent_id]).get(agent_id, TREND_UNKNOWN)


# Bounded ranking nudge derived from the trend. Kept well below the existing
# trust bonus so a trend tilts ties without ever dominating relevance/trust.
_TREND_RANK_BONUS = 2.0


def trend_rank_delta(trend: str) -> float:
    """Pure: small additive ranking term for a trend label (0 when flat/unknown).

    Points scale — for the auto-hire candidate scorer, whose other terms are
    single-digit points.
    """
    if trend == TREND_IMPROVING:
        return _TREND_RANK_BONUS
    if trend == TREND_DECLINING:
        return -_TREND_RANK_BONUS
    return 0.0


# Search blend operates on a normalized 0-1 score (trust weight is 0.12), so the
# trend term there must be far smaller than the points-scale rank delta above.
_TREND_BLEND_BONUS = 0.02


def trend_blend_delta(trend: str) -> float:
    """Pure: tiny additive term for the normalized search blend (0 when flat/unknown)."""
    if trend == TREND_IMPROVING:
        return _TREND_BLEND_BONUS
    if trend == TREND_DECLINING:
        return -_TREND_BLEND_BONUS
    return 0.0
