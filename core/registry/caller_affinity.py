# OWNS: Phase 1 (C3) — small per-caller bias toward agents the caller
#       has already rated well. Reads via the boundary helper in
#       core/reputation.py — never reads job_quality_ratings directly.
# NOT OWNS: scoring orchestration (auto_hire.py); the ratings table
#           itself (core/reputation.py).
# INVARIANTS:
#   - Bias is BOUNDED at ±_AFFINITY_BONUS_CAP (8.0). Cannot outweigh a
#     slug match (+50) or a strong keyword override (+36).
#   - Read-only consumer. No writes.
#   - Cached per (caller_owner_id) with a 5-minute TTL. Same caller
#     in a tight loop doesn't re-query each call.
# DECISIONS:
#   - Five-star rate, not avg rating, drives the bias. Avg of a single
#     1-star rating averages to 1.0; five-star RATE handles small-N
#     more gracefully (one 5-star = 100% five-star rate of n=1, but
#     min-evidence gate filters it out).
# KNOWN DEBT:
#   - Cache is process-local. Across multiple uvicorn workers, each
#     warms its own cache. Acceptable; not worth Redis just for this.
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Bonus cap. Bias is in [-cap, +cap]. Sized so it's a tiebreaker, not
# a hammer.
_AFFINITY_BONUS_CAP = 8.0
# Don't bias from fewer than this many ratings — small N is noisy.
_AFFINITY_MIN_EVIDENCE = 3
# Process-local cache: caller_owner_id -> (timestamp, dict).
# /cso M3 (2026-05-28): bounded LRU keeps memory predictable under
# caller-key churn — without the cap, one entry per unique caller
# would grow indefinitely (1M unique callers × 25 agents × 64 bytes
# = ~1.6 GB before TTL helps).
from collections import OrderedDict as _OrderedDict
_affinity_cache: _OrderedDict[str, tuple[float, dict[str, dict[str, float]]]] = _OrderedDict()
_AFFINITY_CACHE_TTL = 300.0  # 5 minutes
_AFFINITY_CACHE_MAX_ENTRIES = 8192
# Belt-and-suspenders M3 layer 2 (2026-05-29): the GIL serializes
# single bytecode ops but does NOT make multi-op cache mutations
# atomic. `_affinity_cache[key] = ...; _affinity_cache.move_to_end(key)`
# can interleave with concurrent `popitem(last=False)` under FastAPI's
# threadpool, producing a `RuntimeError: OrderedDict mutated during
# iteration`. Lock guards every write path.
_affinity_lock = threading.Lock()


def _get_affinity_data(caller_owner_id: str) -> dict[str, dict[str, float]]:
    """Side-effect: cached fetch of per-caller per-agent ratings.

    Returns the full dict for the caller (all agents they've ever
    rated). Callers then look up specific agent_ids from the result.
    LRU-evicted at ``_AFFINITY_CACHE_MAX_ENTRIES`` so caller-key
    churn can't grow memory unboundedly (/cso M3).
    """
    if not caller_owner_id:
        return {}
    now = time.monotonic()
    with _affinity_lock:
        cached = _affinity_cache.get(caller_owner_id)
        if cached is not None and now - cached[0] < _AFFINITY_CACHE_TTL:
            _affinity_cache.move_to_end(caller_owner_id)
            return cached[1]
    # DB read happens OUTSIDE the lock — could take ms and we don't
    # want to serialize all callers behind one lock during it.
    try:
        from core.reputation import caller_agent_affinity
        data = caller_agent_affinity(caller_owner_id)
    except Exception:  # noqa: BLE001 — never crash scoring
        logger.debug("caller_affinity: fetch failed", exc_info=True)
        with _affinity_lock:
            cached = _affinity_cache.get(caller_owner_id)
        return cached[1] if cached else {}
    with _affinity_lock:
        _affinity_cache[caller_owner_id] = (now, data)
        _affinity_cache.move_to_end(caller_owner_id)
        while len(_affinity_cache) > _AFFINITY_CACHE_MAX_ENTRIES:
            _affinity_cache.popitem(last=False)
    return data


def score_for(
    caller_owner_id: str | None, agent_id: str,
) -> tuple[float, list[str]]:
    """Pure-ish: return (bonus, reasons) for the (caller, agent) pair.

    bonus ∈ [-_AFFINITY_BONUS_CAP, +_AFFINITY_BONUS_CAP].
    Centered at 0 when the caller's five-star rate on this agent
    equals 0.5; positive when they consistently rate it well.
    """
    if not caller_owner_id or not agent_id:
        return 0.0, []
    data = _get_affinity_data(caller_owner_id)
    if agent_id not in data:
        return 0.0, []
    row = data[agent_id]
    rating_count = int(row.get("rating_count", 0))
    if rating_count < _AFFINITY_MIN_EVIDENCE:
        return 0.0, []
    five_star_rate = float(row.get("five_star_rate", 0.0))
    bonus = (five_star_rate - 0.5) * 2.0 * _AFFINITY_BONUS_CAP
    if abs(bonus) < 0.1:
        return 0.0, []
    sign = "+" if bonus > 0 else ""
    return bonus, [
        f"caller affinity: {sign}{bonus:.1f} "
        f"({five_star_rate:.0%} 5-star over {rating_count} ratings)"
    ]


def clear_cache() -> None:
    """Test hook — operators do not call this in production."""
    with _affinity_lock:
        _affinity_cache.clear()


__all__ = ["clear_cache", "score_for"]
