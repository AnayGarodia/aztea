# SPDX-License-Identifier: Apache-2.0
"""
Per-key sliding-window rate limiter (transport layer).

# OWNS: in-memory request-rate accounting per API-key (or IP for anonymous
#       traffic) plus the decision of whether to allow or reject the next
#       request. Provides classification helpers so the middleware in
#       server.application_parts.part_001 stays a thin shim.
# NOT OWNS: authentication (we classify by key *prefix* only, never by DB
#       lookup), business rules around auto-hire trust gates, or any
#       persistence. State lives in process memory and dies with the worker.
# INVARIANTS:
#   - `check_and_record` MUST NOT raise on any input. The caller wraps it
#     in a fail-open try/except, but the limiter itself should never need
#     that safety net to engage. A broken rate limiter that 500s every
#     request is far worse than no rate limiter at all.
#   - All timestamps use `time.monotonic()` — wall-clock jumps (NTP, DST)
#     would otherwise let one second's worth of requests bypass the burst
#     gate or freeze an entry's eviction.
#   - The store is bounded by RATE_LIMIT_MAX_TRACKED_KEYS via LRU eviction
#     so an attacker cycling through synthetic keys cannot OOM the worker.
# DECISIONS:
#   - In-memory only. Production runs two uvicorn workers per host; each
#     keeps its own window. This means the effective limit is roughly
#     2× the constants, which is acceptable for a pre-scale safety gate.
#     Promote to Redis if/when we run more than ~4 workers per region.
#   - Scope is derived from the key prefix (azk_ → worker, master → admin,
#     everything else → caller) so the middleware can run BEFORE auth and
#     skip the DB. A forged azk_ prefix only buys an attacker the more
#     permissive worker bucket; the trust gates still apply downstream.
#   - We sort exempt paths into a frozenset of prefixes and check each
#     request against it. Exempt paths short-circuit before any state read
#     so /health survives a flood with zero accounting overhead.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Iterable, Optional

from core import feature_flags


def _now() -> float:
    """Indirection point so tests can monkeypatch the clock.

    Why: ``time.monotonic`` captured as a default arg would bind at import
    time and dodge monkeypatching; routing every read through this function
    keeps the test surface flat.
    """
    return time.monotonic()


SCOPE_ADMIN = "admin"
SCOPE_WORKER = "worker"
SCOPE_CALLER = "caller"
SCOPE_ANON = "anon"

# Paths exempt from rate limiting. Short-circuit BEFORE any state access so
# health/metrics scrapers never touch the store. Match by exact path or by
# `path.startswith(prefix + "/")`, never raw substring (substring matching
# would let "/api/docs-injection" through unintentionally).
EXEMPT_PATH_PREFIXES: frozenset[str] = frozenset({
    "/health",
    "/metrics",
    "/assets",
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/public/docs",
})

# Window sizes. The minute window is the documented user-facing limit; the
# burst window stops a client from blowing its whole budget in 1 second.
_WINDOW_SECONDS = 60.0
_BURST_WINDOW_SECONDS = 1.0

# Worker keys begin with this prefix in `core/auth/schema.py`. Caller-scoped
# agent keys (`azac_`) and user keys (`az_`) fall into the caller bucket.
_WORKER_KEY_PREFIX = "azk_"

# Cap on stored characters per key. Keys are SHA-prefixed strings in practice;
# 256 chars is generous and bounds the dict's memory consumption.
_MAX_KEY_LENGTH = 256


@dataclass(frozen=True)
class Decision:
    """Outcome of one rate-limit check.

    `allowed=True` means the request can proceed; the timestamp has already
    been recorded. `allowed=False` means reject with HTTP 429 — `retry_after_seconds`
    is the integer count to surface in the Retry-After header.
    """
    allowed: bool
    limit_per_minute: int
    burst_limit_per_second: int
    retry_after_seconds: int


# Module-level state. OrderedDict so the eviction policy can be LRU via
# `move_to_end()` on every access. Each value is a deque of monotonic
# timestamps in ascending order; the oldest sits at index 0.
_STORE: "OrderedDict[str, deque[float]]" = OrderedDict()


def is_path_exempt(path: str) -> bool:
    """Pure: True iff this path is in the documented exempt set."""
    if not path:
        return False
    for prefix in EXEMPT_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def extract_bearer_key(authorization_header: Optional[str]) -> Optional[str]:
    """Pure: parse `Authorization: Bearer <key>`; None for missing/malformed.

    A malformed header (not starting with "Bearer ", empty payload, or token
    longer than _MAX_KEY_LENGTH) is treated as anonymous rather than raising.
    The middleware will then key the rate limit by IP.
    """
    if not authorization_header:
        return None
    if not authorization_header.startswith("Bearer "):
        return None
    raw = authorization_header[7:].strip()
    if not raw or len(raw) > _MAX_KEY_LENGTH:
        return None
    return raw


def classify(bearer_key: Optional[str], master_key: Optional[str]) -> str:
    """Pure: bucket a request into one of four scopes by key shape.

    Admin classification compares against the configured master key in
    constant time so a near-miss does not leak timing info. Worker
    classification is prefix-based to keep this off the DB hot path; a
    forged `azk_` only buys the more permissive worker bucket.
    """
    if bearer_key is None:
        return SCOPE_ANON
    if master_key and _constant_time_equals(bearer_key, master_key):
        return SCOPE_ADMIN
    if bearer_key.startswith(_WORKER_KEY_PREFIX):
        return SCOPE_WORKER
    return SCOPE_CALLER


def _constant_time_equals(a: str, b: str) -> bool:
    """Pure: constant-time string compare. Avoids hmac import on the hot path."""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a.encode("utf-8"), b.encode("utf-8")):
        diff |= x ^ y
    return diff == 0


def limit_for_scope(scope: str) -> tuple[int, int]:
    """Pure: returns (per_minute_limit, burst_per_second_limit) for a scope.

    Admin returns a sentinel (0, 0) — the middleware treats zero limits as
    "always allow" and skips accounting entirely for those callers.
    """
    if scope == SCOPE_ADMIN:
        return (0, 0)
    if scope == SCOPE_WORKER:
        return (feature_flags.RATE_LIMIT_WORKER_RPM, feature_flags.RATE_LIMIT_BURST_RPS)
    if scope == SCOPE_ANON:
        return (feature_flags.RATE_LIMIT_ANON_RPM, feature_flags.RATE_LIMIT_BURST_RPS)
    return (feature_flags.RATE_LIMIT_DEFAULT_RPM, feature_flags.RATE_LIMIT_BURST_RPS)


def _prune_window(timestamps: "deque[float]", now: float) -> None:
    """Side-effect: drop entries older than the minute window from the left."""
    cutoff = now - _WINDOW_SECONDS
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()


def _count_in_burst_window(timestamps: "deque[float]", now: float) -> int:
    """Pure: count entries inside the trailing 1-second window."""
    cutoff = now - _BURST_WINDOW_SECONDS
    count = 0
    for ts in reversed(timestamps):
        if ts >= cutoff:
            count += 1
        else:
            break
    return count


def _evict_lru_if_needed() -> None:
    """Side-effect: enforce the soft cap on tracked keys via LRU eviction."""
    cap = max(1, int(feature_flags.RATE_LIMIT_MAX_TRACKED_KEYS))
    while len(_STORE) > cap:
        _STORE.popitem(last=False)


def check_and_record(
    key: str,
    scope: str,
    *,
    now: Optional[float] = None,
) -> Decision:
    """Side-effect: decide whether to allow this request and (on allow) record it.

    Why: keeping the prune-decide-record sequence atomic in a single call
    is the only way to guarantee burst-window honesty under asyncio's
    cooperative scheduling. Splitting the check from the record would let
    two coroutines both pass the limit check before either had appended.
    """
    if not key or scope == SCOPE_ADMIN:
        # Admin and empty-key requests are always allowed without accounting.
        rpm, burst = limit_for_scope(scope)
        return Decision(
            allowed=True,
            limit_per_minute=rpm,
            burst_limit_per_second=burst,
            retry_after_seconds=0,
        )
    rpm_limit, burst_limit = limit_for_scope(scope)
    current = now if now is not None else _now()
    bucket = _STORE.get(key)
    if bucket is None:
        bucket = deque()
        _STORE[key] = bucket
    else:
        _STORE.move_to_end(key)
    _prune_window(bucket, current)
    if len(bucket) >= rpm_limit:
        oldest = bucket[0]
        retry = max(1, int(_WINDOW_SECONDS - (current - oldest)) + 1)
        return Decision(
            allowed=False,
            limit_per_minute=rpm_limit,
            burst_limit_per_second=burst_limit,
            retry_after_seconds=retry,
        )
    if _count_in_burst_window(bucket, current) >= burst_limit:
        return Decision(
            allowed=False,
            limit_per_minute=rpm_limit,
            burst_limit_per_second=burst_limit,
            retry_after_seconds=1,
        )
    bucket.append(current)
    _evict_lru_if_needed()
    return Decision(
        allowed=True,
        limit_per_minute=rpm_limit,
        burst_limit_per_second=burst_limit,
        retry_after_seconds=0,
    )


def reset_store_for_tests() -> None:
    """Drop all tracked windows. Tests only — never call from production code."""
    _STORE.clear()


def store_size_for_tests() -> int:
    """Read the current store size; for assertion-only use in tests."""
    return len(_STORE)


def store_contains_key_for_tests(key: str) -> bool:
    """Read membership; for assertion-only use in tests."""
    return key in _STORE


def iter_keys_for_tests() -> Iterable[str]:
    """Iterate keys in LRU order (oldest first); for assertion-only use."""
    return list(_STORE.keys())
