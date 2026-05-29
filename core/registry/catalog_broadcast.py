# SPDX-License-Identifier: Apache-2.0
"""
catalog_broadcast.py — process-shared invariant for agent-catalog version.

OWNS: a monotonic counter (``current_version()``) that increments every time
      the agents table mutates, plus a Postgres LISTEN/NOTIFY broadcast so
      sibling worker processes can invalidate their in-memory catalog
      caches in real time.
NOT OWNS: the cache itself (lives next to ``_mcp_active_agents`` in
      ``server/application_parts/part_007.py``) or the mutation calls
      themselves (live in ``core/registry/agents_ops.py``). Mutations must
      end with ``catalog_broadcast.bump()`` — that's the only contract.
INVARIANTS:
  - ``bump()`` is best-effort: a NOTIFY failure must not block the mutation.
    The in-process counter still increments so single-process invalidation
    holds.
  - The Postgres listener runs in one daemon thread per process. The thread
    auto-reconnects on errors with exponential backoff (max 30 s).
  - SQLite backend has no equivalent broadcast. Multi-worker SQLite deploys
    are not supported; a startup assert in part_001 fails the boot if
    WEB_CONCURRENCY > 1 on the SQLite path.
DECISIONS:
  - Channel name: ``aztea_catalog_version``. Payload is the new version int.
  - We do NOT broadcast on every cache miss — that's polling, not
    invalidation. Only mutations broadcast.
  - The version counter is reset to 0 on process start; subscribers compare
    *local* version with the *last-broadcast* version. A version mismatch
    invalidates the cache; the absolute number does not need to agree
    across processes.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

from core import db as _db
from core import observability as _observability

logger = logging.getLogger(__name__)

_CHANNEL_NAME = "aztea_catalog_version"
_LISTENER_RECONNECT_BACKOFF_S = (1, 2, 5, 10, 30)

_lock = threading.Lock()
_version: int = 0
_listener_started: bool = False
_listener_thread: threading.Thread | None = None
_invalidate_callbacks: list[Callable[[int], None]] = []


def current_version() -> int:
    """Return the most recent local version. Cheap; no I/O."""
    with _lock:
        return _version


def bump() -> int:
    """Increment the local version and broadcast via NOTIFY on Postgres.

    Returns the new version. Must be called at the end of every catalog
    mutation. Failures to broadcast are logged at debug — the in-process
    invariant is preserved regardless.
    """
    global _version
    with _lock:
        _version += 1
        new_version = _version
    # Fan out the broadcast outside the lock; we don't need to serialize
    # NOTIFY calls against version reads.
    if _db.IS_POSTGRES:
        try:
            conn = _db.get_raw_connection(_db.DB_PATH)
            conn.execute(f"NOTIFY {_CHANNEL_NAME}, %s", (str(new_version),))
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — broadcast is best-effort
            logger.debug("catalog_broadcast.bump notify failed: %s", exc)
    return new_version


def register_invalidate(callback: Callable[[int], None]) -> None:
    """Subscribe a callback that fires on incoming broadcasts.

    The callback receives the new version int. Callbacks must be cheap and
    must not raise — exceptions are caught and logged.
    """
    with _lock:
        _invalidate_callbacks.append(callback)


def start_listener() -> None:
    """Start the daemon LISTEN thread (Postgres only). Idempotent.

    SQLite backend: no-op; multi-worker SQLite is unsupported (see
    INVARIANTS).
    """
    global _listener_started, _listener_thread
    with _lock:
        if _listener_started:
            return
        if not _db.IS_POSTGRES:
            return
        if _os_disabled():
            logger.info("catalog_broadcast: disabled via AZTEA_CATALOG_BROADCAST_DISABLED")
            return
        _listener_started = True
        _listener_thread = threading.Thread(
            target=_listener_loop,
            name="aztea-catalog-broadcast",
            daemon=True,
        )
        _listener_thread.start()


def stop_listener() -> None:
    """Mark the listener stopped. The daemon thread exits on next reconnect cycle."""
    global _listener_started
    with _lock:
        _listener_started = False


def _listener_loop() -> None:
    """Long-running: connect, LISTEN, dispatch notifications. Reconnects on error."""
    backoff_idx = 0
    while True:
        with _lock:
            if not _listener_started:
                return
        try:
            _run_one_listener_session()
            # Healthy exit (server shutdown) — leave the loop.
            return
        except Exception as exc:  # noqa: BLE001 — never crash the daemon
            backoff = _LISTENER_RECONNECT_BACKOFF_S[
                min(backoff_idx, len(_LISTENER_RECONNECT_BACKOFF_S) - 1)
            ]
            logger.warning(
                "catalog_broadcast: listener errored (%s); retrying in %ss",
                type(exc).__name__,
                backoff,
            )
            try:
                _observability.catalog_broadcast_reconnects_total.inc()
            except Exception:  # pragma: no cover
                pass
            time.sleep(backoff)
            backoff_idx += 1


_LISTENER_KEEPALIVE_INTERVAL_S = 60.0


def _run_one_listener_session() -> None:
    """Open one psycopg2 LISTEN connection and pump notifications until EOF."""
    # Late-import psycopg2 so SQLite-only test runs never touch it.
    import psycopg2  # type: ignore
    import select

    # The pooled connection manager normalizes %s/? but the LISTEN connection
    # must be a raw psycopg2 connection so notifies surface on .notifies. We
    # read the DSN from env directly — DB_PATH is the SQLite path on the
    # sqlite backend and a DSN here.
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        # Pragma: this should be impossible (we already checked IS_POSTGRES),
        # but bail gracefully.
        logger.warning("catalog_broadcast: DATABASE_URL empty; skipping listener")
        return
    conn = psycopg2.connect(dsn)
    try:
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()
        cur.execute(f"LISTEN {_CHANNEL_NAME}")
        last_keepalive = time.monotonic()
        while True:
            with _lock:
                if not _listener_started:
                    return
            # Poll up to 5 s so we re-check _listener_started periodically.
            ready = select.select([conn], [], [], 5.0) != ([], [], [])
            if ready:
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    _handle_notify(notify.payload)
            # Application-level keepalive: a NAT/load balancer with an idle
            # timeout (typically 5-30 min) can silently drop the connection
            # without firing an error on select. ``SELECT 1`` every 60 s
            # forces traffic on the socket; if the connection died, this
            # raises and the outer loop reconnects. The query itself is
            # negligible cost on the DB side.
            now = time.monotonic()
            if (now - last_keepalive) >= _LISTENER_KEEPALIVE_INTERVAL_S:
                cur.execute("SELECT 1")
                _ = cur.fetchone()
                last_keepalive = now
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


_MAX_NOTIFY_VERSION_JUMP = 1000


def _handle_notify(payload: str) -> None:
    """Promote local version to the broadcast version and fire callbacks.

    Defense against forged NOTIFY payloads (anyone with DB write access can
    issue ``NOTIFY aztea_catalog_version, '<arbitrary>'``): we bound how
    far a single broadcast can advance the local version, and refuse
    non-positive payloads. The cache-invalidation callback still fires
    regardless of the value — the worst a forged payload can do is force
    one extra rebuild. We clamp the version advance so a pathological
    payload (e.g. ``'9999999999'``) does not silently confuse downstream
    decision-cache keys.
    """
    global _version
    try:
        incoming = int(payload)
    except (TypeError, ValueError):
        logger.debug("catalog_broadcast: ignored non-integer NOTIFY payload")
        return
    if incoming <= 0:
        logger.debug("catalog_broadcast: ignored non-positive NOTIFY payload %d", incoming)
        return
    with _lock:
        # Bound the version advance. Legitimate mutations bump by 1; even
        # with high concurrency across N workers a single broadcast cannot
        # legitimately advance by more than ~few-hundred. A 1000-unit jump
        # indicates a malformed or spoofed payload.
        if incoming - _version > _MAX_NOTIFY_VERSION_JUMP:
            logger.warning(
                "catalog_broadcast: suspicious version jump %d -> %d in NOTIFY "
                "(payload spoofed or DB clock skew?); clamping advance to +1",
                _version,
                incoming,
            )
            incoming = _version + 1
        if incoming > _version:
            _version = incoming
        callbacks = list(_invalidate_callbacks)
    for cb in callbacks:
        try:
            cb(incoming)
        except Exception as exc:  # noqa: BLE001
            logger.debug("catalog_broadcast: invalidate callback failed: %s", exc)


def _os_disabled() -> bool:
    return os.environ.get("AZTEA_CATALOG_BROADCAST_DISABLED", "").strip().lower() in {"1", "true"}
