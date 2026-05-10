"""In-memory TTL cache for workspace bundles, keyed by content fingerprint.

# OWNS: A short-lived (default 10 min), process-local map from bundle
#       fingerprint → bundle payload, so the MCP can ship a 64-char
#       fingerprint on subsequent calls instead of re-shipping ~5KB.
# NOT OWNS: The bundle itself (core/workspace_bundle.py) or persistence — the
#           cache is deliberately ephemeral; restart clears it.
# INVARIANTS:
#   - Bundles are never written to disk or to the database from this module.
#   - Expired entries are evicted lazily on read.
#   - The cache is bounded by MAX_ENTRIES; oldest entries are dropped on overflow.
# DECISIONS:
#   - Process-local memory rather than Redis: workspace bundles are tiny,
#     low-traffic, and rebuilding one on a cache miss is cheap. Redis would
#     add a network hop for no benefit.
"""

from __future__ import annotations

import threading
import time
from typing import Any

DEFAULT_TTL_SECONDS = 600
MAX_ENTRIES = 256


class _BundleCache:
    """Thread-safe in-memory cache. Single instance lives at module scope."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        if not fingerprint:
            return None
        with self._lock:
            entry = self._entries.get(fingerprint)
            if entry is None:
                return None
            expires_at, payload = entry
            if expires_at < time.monotonic():
                self._entries.pop(fingerprint, None)
                return None
            return payload

    def put(
        self,
        fingerprint: str,
        payload: dict[str, Any],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if not fingerprint:
            return
        ttl = max(1, int(ttl_seconds or DEFAULT_TTL_SECONDS))
        with self._lock:
            self._entries[fingerprint] = (
                time.monotonic() + ttl,
                dict(payload),
            )
            if len(self._entries) > MAX_ENTRIES:
                self._evict_oldest_locked()

    def _evict_oldest_locked(self) -> None:
        """Drop entries with the soonest expiry until we are back under the cap."""
        ordered = sorted(self._entries.items(), key=lambda kv: kv[1][0])
        for fingerprint, _ in ordered[: len(self._entries) - MAX_ENTRIES]:
            self._entries.pop(fingerprint, None)

    def clear(self) -> None:
        """Drop all entries. Test-only helper; production callers do not use this."""
        with self._lock:
            self._entries.clear()


_CACHE = _BundleCache()


def cache_workspace_bundle(
    fingerprint: str,
    payload: dict[str, Any],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    """Store a workspace bundle payload under its fingerprint."""
    _CACHE.put(fingerprint, payload, ttl_seconds)


def get_workspace_bundle(fingerprint: str) -> dict[str, Any] | None:
    """Look up a workspace bundle payload by fingerprint; None on miss/expiry."""
    return _CACHE.get(fingerprint)


def _reset_for_tests() -> None:
    """Drop all entries. Tests only."""
    _CACHE.clear()
