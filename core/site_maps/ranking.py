"""Pure reputation-weighted ranking for competing site maps.

# OWNS: the read-time scoring that picks the winning map when several authors
#        have mapped the same site_key.
# NOT OWNS: trust-score computation (core.reputation supplies trust_by_agent),
#           DB access, or freshness validation.
# DECISIONS: weights are quality-dominant (trust + empirical reliability beat
#   recency), mirroring core/reputation.py's philosophy. Tunable here.
"""

from __future__ import annotations

import math
import time
from typing import Any

from core.site_maps import normalize as _normalize

# Quality-dominant blend; sums to 1.0 before the (subtractive) challenge penalty.
_W_TRUST = 0.45
_W_RELIABILITY = 0.35
_W_RECENCY = 0.20
# Each open challenge subtracts this from the score. The score is consumed only
# for relative ordering (select_best_map), so it is intentionally unbounded-below
# — enough open challenges drive a map's score negative and sink it under all others.
_CHALLENGE_PENALTY = 0.25
# Recency half-life: a map validated this long ago scores 0.5 on the recency term.
_RECENCY_HALF_LIFE_SECONDS = 7 * 24 * 3600.0


def _parse_iso(ts: str | None) -> float | None:
    """Pure: delegate to the shared, tz-safe parser (one impl in normalize.py)."""
    return _normalize.parse_iso_to_epoch(ts)


def _recency_score(map_row: dict[str, Any], now_epoch: float) -> float:
    """Pure: exponential-decay recency in [0,1] from last_validated_at (or created_at)."""
    when = _parse_iso(map_row.get("last_validated_at")) or _parse_iso(map_row.get("created_at"))
    if when is None:
        return 0.0
    age = max(0.0, now_epoch - when)
    return math.pow(0.5, age / _RECENCY_HALF_LIFE_SECONDS)


def _reliability_score(map_row: dict[str, Any]) -> float:
    """Pure: empirical fresh-vs-drift ratio, Laplace-smoothed into [0,1)."""
    fresh = max(0, int(map_row.get("fresh_validation_count") or 0))
    drift = max(0, int(map_row.get("drift_count") or 0))
    return fresh / (fresh + drift + 1)


def score_map(
    map_row: dict[str, Any],
    *,
    trust_by_agent: dict[str, float],
    open_challenges_by_map: dict[str, int],
    now_epoch: float,
) -> float:
    """Pure: blended score for one map. Higher is better."""
    trust = float(trust_by_agent.get(str(map_row.get("author_agent_id") or ""), 0.0))
    trust_term = max(0.0, min(trust / 100.0, 1.0))  # reputation is 0-100
    challenges = int(open_challenges_by_map.get(str(map_row.get("map_id") or ""), 0))
    return (
        _W_TRUST * trust_term
        + _W_RELIABILITY * _reliability_score(map_row)
        + _W_RECENCY * _recency_score(map_row, now_epoch)
        - _CHALLENGE_PENALTY * challenges
    )


def rank_maps(
    maps: list[dict[str, Any]],
    *,
    trust_by_agent: dict[str, float],
    open_challenges_by_map: dict[str, int] | None = None,
    now_epoch: float | None = None,
) -> list[dict[str, Any]]:
    """Pure: maps sorted best-first by blended score. Stable on ties (map_id)."""
    now = now_epoch if now_epoch is not None else time.time()
    challenges = open_challenges_by_map or {}
    scored = [
        (
            score_map(m, trust_by_agent=trust_by_agent, open_challenges_by_map=challenges, now_epoch=now),
            str(m.get("map_id") or ""),
            m,
        )
        for m in maps
    ]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [m for _, _, m in scored]


def select_best_map(
    maps: list[dict[str, Any]],
    *,
    trust_by_agent: dict[str, float],
    open_challenges_by_map: dict[str, int] | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any] | None:
    """Pure: the single highest-ranked map, or None when there are none."""
    ranked = rank_maps(
        maps, trust_by_agent=trust_by_agent,
        open_challenges_by_map=open_challenges_by_map, now_epoch=now_epoch,
    )
    return ranked[0] if ranked else None
