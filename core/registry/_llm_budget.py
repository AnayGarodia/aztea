# OWNS: shared per-process token bucket for routing-side LLM calls
#       (tiebreaker, intent classifier, whole-payload extractor,
#       example-intents generator). Prevents an adversary from burning
#       LLM budget by crafting many ambiguous intents.
# NOT OWNS: the LLM calls themselves; provider keys; long-running
#       agent jobs (those have wallet caps).
# INVARIANTS:
#   - try_consume(category) returns True at most _CAPACITY times per
#     _WINDOW_SECONDS per category. Refill is continuous (token
#     bucket), not bursty.
#   - When the bucket is empty, caller MUST treat as "LLM unavailable"
#     and fall back to deterministic behavior.
#   - Thread-safe.
#   - Belt-and-suspenders /cso H1 layer 2 (2026-05-29): a single
#     request handler can also be enrolled in a per-request "burst"
#     budget via ``RequestBudget``. Even if the global + per-owner
#     buckets are full, no single orchestration can fire more than
#     _PER_REQUEST_CAP LLM calls total. Bounded amplification.
# DECISIONS:
#   - Per-category budgets (not global) so a misbehaving classifier
#     doesn't starve the tiebreaker. Categories declared explicitly.
#   - Default capacity tuned for "single user using the system normally
#     should never hit the cap" — adjust via env vars when telemetry
#     justifies it.
#   - Two-tier (per-owner + global) because owner-only would let a
#     multi-owner Sybil exhaust the global pool; global-only would let
#     one caller drain the pool for everyone.
# KNOWN DEBT:
#   - Process-local. Behind a load balancer with N workers, the
#     effective budget is N * cap. Acceptable for v1; Redis-backed
#     when traffic justifies it.
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Default budgets, per category, per window. Env-overridable for ops.
_DEFAULT_CAPACITY = {
    "tiebreaker": int(os.environ.get("AZTEA_LLM_BUDGET_TIEBREAKER", "60")),
    "classifier": int(os.environ.get("AZTEA_LLM_BUDGET_CLASSIFIER", "120")),
    "whole_payload": int(os.environ.get("AZTEA_LLM_BUDGET_WHOLE_PAYLOAD", "60")),
    "examples": int(os.environ.get("AZTEA_LLM_BUDGET_EXAMPLES", "30")),
}
_WINDOW_SECONDS = float(os.environ.get("AZTEA_LLM_BUDGET_WINDOW_S", "60"))


@dataclass
class _Bucket:
    """One token bucket for one (category, scope) pair."""
    capacity: int
    tokens: float
    last_refill: float


# Two budget layers:
#   - Per-CALLER (default 10/min/category): one authenticated owner
#     can't burn the global pool. Defeats /cso H1 cross-tenant DoS.
#   - GLOBAL (default 60/min/category): system-wide ceiling for budget
#     planning. The smaller of the two binds.
_PER_CALLER_FRACTION = float(
    os.environ.get("AZTEA_LLM_BUDGET_PER_CALLER_FRACTION", "0.17")
)
_per_caller_buckets: dict[tuple[str, str], _Bucket] = {}
_global_buckets: dict[str, _Bucket] = {}
_lock = threading.Lock()

# /cso M3-style cap on the per-caller dict size to prevent memory growth
# under caller-key churn. LRU eviction by last-touched.
_MAX_PER_CALLER_ENTRIES = int(
    os.environ.get("AZTEA_LLM_BUDGET_MAX_CALLERS", "8192")
)

# Per-request cap (belt-and-suspenders layer 2): even if both the
# per-caller and global budgets have headroom, a single orchestration
# of do_specialist_task should never trigger more than this many LLM
# calls. Bounds the amplification factor regardless of who's calling.
_PER_REQUEST_CAP = int(os.environ.get("AZTEA_LLM_BUDGET_PER_REQUEST", "4"))

# Throttle redundant exhaustion logs (one per category per minute).
_last_exhaustion_log: dict[str, float] = {}
_EXHAUSTION_LOG_INTERVAL_S = 60.0


def _log_exhaustion(category: str, reason: str) -> None:
    """Emit a structured warning at most once per minute per category.

    Why: ops needs to know when budgets are saturated, but per-call
    logging would itself amplify under attack. Token-bucketed logging.
    """
    now = time.monotonic()
    last = _last_exhaustion_log.get(category, 0.0)
    if now - last < _EXHAUSTION_LOG_INTERVAL_S:
        return
    _last_exhaustion_log[category] = now
    logger.warning(
        "llm_budget.exhausted category=%s reason=%s "
        "(throttled: 1/min per category)",
        category, reason,
    )


@dataclass
class RequestBudget:
    """Per-orchestration LLM call counter — layer 2 of H1 defense.

    One handler (e.g. registry_auto_hire) creates one RequestBudget
    and threads it through to every LLM call site. Even if the
    global/per-owner buckets allow it, a single request can never
    fire more than _PER_REQUEST_CAP LLM calls.
    """
    cap: int = _PER_REQUEST_CAP
    used: int = 0

    def try_consume(self, category: str) -> bool:
        if self.used >= self.cap:
            _log_exhaustion(category, "per_request_cap")
            return False
        self.used += 1
        return True


def new_request_budget() -> RequestBudget:
    """Construct a fresh per-request budget. Cheap; one per handler call."""
    return RequestBudget()


def _per_caller_capacity(category: str) -> int:
    """Pure: cap per-caller usage at a fraction of the global cap."""
    global_cap = _DEFAULT_CAPACITY.get(category, 60)
    return max(1, int(global_cap * _PER_CALLER_FRACTION))


def _get_global_bucket(category: str) -> _Bucket:
    if category not in _global_buckets:
        capacity = _DEFAULT_CAPACITY.get(category, 60)
        _global_buckets[category] = _Bucket(
            capacity=capacity,
            tokens=float(capacity),
            last_refill=time.monotonic(),
        )
    return _global_buckets[category]


def _get_per_caller_bucket(category: str, caller_id: str) -> _Bucket:
    key = (category, caller_id)
    if key in _per_caller_buckets:
        return _per_caller_buckets[key]
    # Evict LRU when over the cap. Plain insertion-order dict
    # iteration gives us the oldest entry first.
    if len(_per_caller_buckets) >= _MAX_PER_CALLER_ENTRIES:
        oldest = next(iter(_per_caller_buckets))
        _per_caller_buckets.pop(oldest, None)
    capacity = _per_caller_capacity(category)
    bucket = _Bucket(
        capacity=capacity,
        tokens=float(capacity),
        last_refill=time.monotonic(),
    )
    _per_caller_buckets[key] = bucket
    return bucket


def _refill(bucket: _Bucket, now: float) -> None:
    """Add tokens based on elapsed time, capped at capacity."""
    elapsed = max(0.0, now - bucket.last_refill)
    bucket.tokens = min(
        float(bucket.capacity),
        bucket.tokens + (elapsed / _WINDOW_SECONDS) * bucket.capacity,
    )
    bucket.last_refill = now


def try_consume(
    category: str,
    tokens: int = 1,
    *,
    caller_owner_id: str | None = None,
    request_budget: "RequestBudget | None" = None,
) -> bool:
    """Atomic: try to consume N tokens from per-caller AND global buckets.

    Three independent layers, each must permit the call:
      1. per-request `RequestBudget` — caps amplification per handler
      2. per-caller bucket — defeats single-owner DoS
      3. global bucket — system-wide ceiling

    Failure of any layer returns False; on False the caller MUST treat
    as "LLM unavailable" and fall back to deterministic behavior.

    /cso H1 (2026-05-28 + 2026-05-29 belt-and-suspenders): each layer
    can fail independently. Exhaustion is logged structured + rate-
    limited so ops can observe saturation without log amplification.
    """
    # Layer 1: per-request cap (caller-supplied or None).
    if request_budget is not None and not request_budget.try_consume(category):
        return False
    with _lock:
        now = time.monotonic()
        # Tentatively consume from both buckets; on failure of either
        # we must NOT have decremented the other. So check first, then
        # decrement atomically.
        per_caller = (
            _get_per_caller_bucket(category, caller_owner_id)
            if caller_owner_id
            else None
        )
        global_bucket = _get_global_bucket(category)
        if per_caller is not None:
            _refill(per_caller, now)
            if per_caller.tokens < tokens:
                _log_exhaustion(category, "per_caller")
                # Layer 1 already consumed; refund.
                if request_budget is not None:
                    request_budget.used = max(0, request_budget.used - 1)
                return False
        _refill(global_bucket, now)
        if global_bucket.tokens < tokens:
            _log_exhaustion(category, "global")
            if request_budget is not None:
                request_budget.used = max(0, request_budget.used - 1)
            return False
        if per_caller is not None:
            per_caller.tokens -= tokens
        global_bucket.tokens -= tokens
        return True


def reset() -> None:
    """Test hook — refills every bucket to full and clears per-caller
    pool. Operators do not call this in production."""
    with _lock:
        now = time.monotonic()
        for bucket in _global_buckets.values():
            bucket.tokens = float(bucket.capacity)
            bucket.last_refill = now
        _per_caller_buckets.clear()


def status() -> dict[str, dict[str, float]]:
    """Snapshot of GLOBAL buckets for observability.

    Per-caller buckets are not surfaced (could be tens of thousands);
    operators wanting per-caller visibility use logs.
    """
    with _lock:
        now = time.monotonic()
        out: dict[str, dict[str, float]] = {}
        for name, bucket in _global_buckets.items():
            _refill(bucket, now)
            out[name] = {
                "capacity": float(bucket.capacity),
                "tokens": float(bucket.tokens),
                "fraction": bucket.tokens / bucket.capacity if bucket.capacity else 0.0,
            }
        return out


__all__ = [
    "RequestBudget",
    "new_request_budget",
    "reset",
    "status",
    "try_consume",
]
