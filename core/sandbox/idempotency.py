"""Idempotency-key dedup cache for sandbox mutating actions.

# OWNS: a process-local LRU cache keyed on (action, idempotency_key) so
#       retries of the same logical operation return the same response
#       — including the same receipt — rather than re-executing.
# NOT OWNS: persistence across process restarts. The cache is in-memory
#           by design; an MCP session that restarts loses its dedup
#           history. That's the right v0 trade-off — the alternative is
#           putting receipts in the DB which is a much bigger lift.
# INVARIANTS:
#   * Only mutating actions are eligible (declared in _MUTATING_ACTIONS).
#     Read-only actions don't need dedup.
#   * Cache is bounded by _CACHE_MAX_ENTRIES; oldest entries evict first.
#   * Entries expire after _CACHE_TTL_SECONDS.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

_CACHE_MAX_ENTRIES = 1024
_CACHE_TTL_SECONDS = 3600  # 1h — longer than any reasonable retry window

# Actions where idempotent retry is meaningful. Read-only verbs are
# excluded — replaying them is harmless either way and we don't want to
# cache a stale snapshot of e.g. sandbox_status.
_MUTATING_ACTIONS = frozenset({
    "sandbox_start",
    "sandbox_stop",
    "sandbox_extend",
    "sandbox_resume",
    "sandbox_batch_start",
    "sandbox_exec",
    "sandbox_exec_in_service",
    "sandbox_bg_start",
    "sandbox_bg_kill",
    "sandbox_write_file",
    "sandbox_delete_file",
    "sandbox_apply_patch",
    "sandbox_sync_from_local",
    "sandbox_db_snapshot",
    "sandbox_db_restore",
    "sandbox_db_seed",
    "sandbox_snapshot",
    "sandbox_restore",
    "sandbox_fork",
    "sandbox_outbound_record",
    "sandbox_outbound_replay",
    "sandbox_inject_failure",
    "sandbox_link",
    "sandbox_export_snapshot",
    "sandbox_tunnel_open",
    "sandbox_tunnel_close",
    "sandbox_share",
    "sandbox_network_capture",
    "sandbox_trace",
})


_CacheKey = tuple[str, str]
_CacheEntry = tuple[float, dict[str, Any]]

_CACHE: OrderedDict[_CacheKey, _CacheEntry] = OrderedDict()
_LOCK = threading.RLock()


def lookup(action: str, key: str | None) -> dict[str, Any] | None:
    """Return a cached response for ``(action, key)`` if present + fresh.

    Why: every dispatch call asks here first. A hit returns the original
    response with a ``replayed=true`` flag added so observability tools
    know the work didn't run twice.
    """
    if not key or action not in _MUTATING_ACTIONS:
        return None
    ck = (action, key)
    now = time.time()
    with _LOCK:
        entry = _CACHE.get(ck)
        if entry is None:
            return None
        ts, response = entry
        if now - ts > _CACHE_TTL_SECONDS:
            _CACHE.pop(ck, None)
            return None
        # Refresh LRU position.
        _CACHE.move_to_end(ck)
    # Return a copy so callers mutating their response don't corrupt
    # the cache state.
    replayed = dict(response)
    replayed["idempotency_replayed"] = True
    return replayed


def store(action: str, key: str | None, response: dict[str, Any]) -> None:
    """Cache a successful response under ``(action, key)``.

    Why: stored after the handler completes successfully so a retry
    returns the same body (and the same receipt hash). Errors are NOT
    cached — they should be retried because the underlying state may
    have changed.
    """
    if not key or action not in _MUTATING_ACTIONS:
        return
    if not isinstance(response, dict):
        return
    if "error" in response:
        # Don't cache failures; retry semantics expect them to be re-run.
        return
    ck = (action, key)
    with _LOCK:
        _CACHE[ck] = (time.time(), dict(response))
        _CACHE.move_to_end(ck)
        while len(_CACHE) > _CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)


def reset_for_tests() -> None:
    """Side-effect: clear the cache. Tests only."""
    with _LOCK:
        _CACHE.clear()


def stats() -> dict[str, Any]:
    """Pure: snapshot the cache stats for ``sandbox_status`` or operator probes."""
    with _LOCK:
        return {
            "entries": len(_CACHE),
            "max_entries": _CACHE_MAX_ENTRIES,
            "ttl_seconds": _CACHE_TTL_SECONDS,
            "mutating_actions": sorted(_MUTATING_ACTIONS),
        }
