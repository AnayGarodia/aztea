"""Short-lived in-memory cache for the public agent catalog.

# OWNS: caching of ``client.list_agents()`` results so the REPL can
#        re-render /agents without re-hitting the network on every
#        invocation.
# NOT OWNS: the HTTP call itself (lives in ``AzteaClient.list_agents``),
#            search results (each query is unique — never cached),
#            anything personalised (wallet, jobs, …).
# INVARIANTS:
#   - TTL is intentionally short (``_AGENTS_TTL_S`` = 60s). The catalog
#     can change when admins add or retire agents; stale-by-up-to-60s
#     is acceptable for browse UX, longer is not.
#   - Lifetime = process. Each ``aztea`` invocation starts cold. Only
#     useful for long-lived processes like the REPL.
#   - Reads are lock-free (atomic dict reads); writes hold the lock.
#     Worst case under a race is a redundant duplicate fetch, never a
#     corrupted cache entry.
# DECISIONS:
#   - Prewarm uses a background thread so REPL startup isn't blocked.
#     Failures are silent — the next /agents call retries inline.
#   - Caches only the unfiltered, default-rank ``list_agents()`` call.
#     Filtered variants (search, tag, custom rank) hit the network
#     each time so their cache key doesn't have to encode every option.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional


_AGENTS_TTL_S = 60.0


_state: dict = {
    "ts": 0.0,
    "data": None,
    "lock": threading.Lock(),
    "in_flight": False,
}


def fresh() -> bool:
    """True iff the cache holds a non-expired catalog snapshot."""
    return (
        _state["data"] is not None
        and (time.monotonic() - _state["ts"]) < _AGENTS_TTL_S
    )


def get_cached() -> Optional[list]:
    """Return the cached agent list, or None when stale / missing."""
    return _state["data"] if fresh() else None


def store(data: list) -> None:
    """Replace the cache contents atomically."""
    with _state["lock"]:
        _state["data"] = data
        _state["ts"] = time.monotonic()


def clear() -> None:
    """Wipe the cache. Used by tests; not wired into any product path."""
    with _state["lock"]:
        _state["data"] = None
        _state["ts"] = 0.0
        _state["in_flight"] = False


def prewarm() -> None:
    """Kick off a background fetch + cache write. Never raises.

    Safe to call multiple times — concurrent prewarms are deduped via
    the ``in_flight`` flag. Called from the REPL's ``start()`` so the
    user's first /agents typically lands on a warm cache.

    The thread is a daemon so it does not delay process shutdown.
    """
    if fresh() or _state["in_flight"]:
        return
    _state["in_flight"] = True

    threading.Thread(target=_prewarm_worker, daemon=True).start()


def _prewarm_worker() -> None:
    """Run the prewarm HTTP call. Failures leave the cache untouched."""
    try:
        from ..client import AzteaClient
        from ..config import load_config
        cfg = load_config() or {}
        base_url = cfg.get("base_url") or "https://aztea.ai"
        api_key = cfg.get("api_key")
        client = AzteaClient(
            base_url=base_url,
            api_key=api_key,
            client_id="aztea-repl-prewarm",
        )
        agents = client.list_agents()
        store(agents)
    except Exception:
        # Silent: foreground /agents will retry. Logging here would
        # land in stderr during REPL startup — we'd rather stay quiet
        # and let the visible failure surface only if the user actually
        # runs /agents while still offline.
        pass
    finally:
        _state["in_flight"] = False


def get_or_fetch(client: Any) -> list:
    """Return cached data when fresh; otherwise fetch and cache.

    The ``client`` argument is whatever ``aztea.cli.agents._open_client()``
    yielded — we only call ``.list_agents()`` on it, so any object
    matching that contract works (the test suite passes a fake).
    """
    cached = get_cached()
    if cached is not None:
        return cached
    fetched = client.list_agents()
    store(fetched)
    return fetched
