# SPDX-License-Identifier: Apache-2.0
"""
deferred.py — bounded background queue for observability-only writes.

OWNS: a single in-process queue + daemon worker that drains it. Used to move
      writes that don't gate the request response off the request thread
      (decision_audit, receipt_write, work_example).
NOT OWNS: any write that affects wallet balance, ledger rows, or job status.
      Those stay synchronous in the request handler. Money correctness is
      not negotiable; observability bounded loss is.
INVARIANTS:
  - Drops oldest items on overflow. The drop fact is signaled per-caller on
    the next response via the request handler when applicable, and counted
    via ``deferred_drops_total{name}``.
  - The worker owns its own DB connections (opened inside the dequeue
    loop). Closures must NOT capture connections from the request thread.
  - ``flush(timeout)`` drains pending items via a sentinel; mirrors the
    sweeper-thread pattern in part_006.
DECISIONS:
  - Caller-tagged enqueue (``caller_owner_id``) so a single noisy caller
    doesn't drop other callers' rows. Head-of-line drop at 25 % share of
    queue depth (see _drop_policy).
  - Bounded queue maxsize=8192. Tunable via env
    ``AZTEA_DEFERRED_QUEUE_MAXSIZE``.
  - No durable fallback. Engineering review proved the proposed
    SQLite-backed fallback design was broken; we accept honest best-effort
    instead of duct-taped durability. Loss bound: items in-queue at crash
    time.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from core import observability as _observability

logger = logging.getLogger(__name__)

_DEFAULT_MAXSIZE = 8192
_HOL_DROP_CALLER_THRESHOLD = 0.25  # 25 % of queue items from one caller → drop theirs first


@dataclass(frozen=True)
class _Item:
    name: str
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    enqueued_at: float
    caller_owner_id: str | None


# Module-level state. Re-entrant: start()/flush() are idempotent.
_lock = threading.Lock()
_q: queue.Queue[_Item | None] | None = None
_worker: threading.Thread | None = None
_started: bool = False


def _maxsize() -> int:
    raw = os.environ.get("AZTEA_DEFERRED_QUEUE_MAXSIZE", "").strip()
    if not raw:
        return _DEFAULT_MAXSIZE
    try:
        v = int(raw)
        return v if v > 0 else _DEFAULT_MAXSIZE
    except ValueError:
        return _DEFAULT_MAXSIZE


def _ensure_queue() -> queue.Queue[_Item | None]:
    global _q
    with _lock:
        if _q is None:
            _q = queue.Queue(maxsize=_maxsize())
        return _q


def start() -> None:
    """Start the daemon worker. Idempotent."""
    global _worker, _started
    with _lock:
        if _started:
            return
        _started = True
    _ensure_queue()
    with _lock:
        _worker = threading.Thread(
            target=_loop,
            name="aztea-deferred-worker",
            daemon=True,
        )
        _worker.start()


def enqueue(
    name: str,
    fn: Callable[..., Any],
    *args: Any,
    caller_owner_id: str | None = None,
    **kwargs: Any,
) -> bool:
    """Enqueue one item. Returns True on success, False on drop.

    Drop policy: queue full → drop *this* item unless a single caller is
    monopolising the queue, in which case drop their oldest item to make
    room for this one (head-of-line protection).
    """
    item = _Item(
        name=name,
        fn=fn,
        args=args,
        kwargs=kwargs,
        enqueued_at=time.monotonic(),
        caller_owner_id=caller_owner_id,
    )
    q = _ensure_queue()
    try:
        q.put_nowait(item)
        return True
    except queue.Full:
        # Try head-of-line drop: if any single caller is dominating the
        # queue, drop their oldest item, then re-enqueue this one.
        if _try_head_of_line_drop(q, caller_owner_id):
            try:
                q.put_nowait(item)
                return True
            except queue.Full:
                pass
        try:
            _observability.deferred_drops_total.labels(name=name, reason="queue_full").inc()
        except Exception:  # pragma: no cover
            pass
        return False


def _try_head_of_line_drop(
    q: queue.Queue[_Item | None],
    incoming_caller: str | None,
) -> bool:
    """Drop the oldest item from a caller that exceeds the share threshold.

    Returns True if room was made. Best-effort: examines a snapshot of
    in-flight items; under heavy concurrency the share count is approximate.
    """
    snapshot = list(q.queue)  # internal access — acceptable for inspection
    if not snapshot:
        return False
    counts = Counter(item.caller_owner_id for item in snapshot if item is not None)
    if not counts:
        return False
    top_caller, top_count = counts.most_common(1)[0]
    if top_caller is None or top_count / len(snapshot) < _HOL_DROP_CALLER_THRESHOLD:
        return False
    if top_caller == incoming_caller:
        return False  # don't penalize the same caller for their own admission
    # Drop one of the top_caller's items: walk the underlying deque and
    # remove the first match. ``victim`` stays None if the worker drained
    # the matching items between the snapshot and the mutex — we report
    # "no room made" in that case rather than NameError'ing.
    victim: _Item | None = None
    try:
        with q.mutex:  # internal access — guarded by the queue's own lock
            for candidate in list(q.queue):
                if candidate is not None and candidate.caller_owner_id == top_caller:
                    q.queue.remove(candidate)
                    victim = candidate
                    break
    except (ValueError, AttributeError):
        return False
    if victim is None:
        # Snapshot was stale — the noisy caller's items already drained.
        return False
    try:
        _observability.deferred_drops_by_caller_total.labels(
            caller_owner_id=str(top_caller)
        ).inc()
        _observability.deferred_drops_total.labels(
            name=victim.name,
            reason="head_of_line",
        ).inc()
    except Exception:  # pragma: no cover
        pass
    return True


def flush(timeout: float = 5.0) -> int:
    """Drain pending items via sentinel; wait up to ``timeout`` seconds.

    Returns the count of items still in the queue when the timeout fires
    (0 if drained cleanly). Safe to call from shutdown.
    """
    global _started
    q = _ensure_queue()
    with _lock:
        if not _started:
            return q.qsize()
        _started = False
        worker = _worker
    # Signal the worker to exit after draining current items.
    try:
        q.put_nowait(None)
    except queue.Full:
        # Queue is full; force-drain a slot by popping one.
        try:
            q.get_nowait()
            q.put_nowait(None)
        except queue.Empty:
            pass
    if worker is not None:
        worker.join(timeout=timeout)
    return q.qsize()


def _loop() -> None:
    """Daemon worker: dequeue → call fn → mark processed. Never raises out."""
    q = _ensure_queue()
    while True:
        try:
            item = q.get()
        except Exception:  # pragma: no cover
            continue
        if item is None:
            # Sentinel from flush(): exit cleanly.
            return
        try:
            lag = max(0.0, time.monotonic() - item.enqueued_at)
            _observability.deferred_lag_seconds.labels(name=item.name).observe(lag)
        except Exception:  # pragma: no cover
            pass
        try:
            item.fn(*item.args, **item.kwargs)
            try:
                _observability.deferred_processed_total.labels(name=item.name).inc()
            except Exception:  # pragma: no cover
                pass
        except Exception as exc:  # noqa: BLE001 — must not crash the worker
            logger.warning(
                "deferred: handler %s raised: %s", item.name, exc, exc_info=True
            )


# ── Test hooks ────────────────────────────────────────────────────────────


def _reset_for_tests() -> None:
    """Re-initialize state. Test-only — never call from production code."""
    global _q, _worker, _started
    with _lock:
        _started = False
        _q = None
        _worker = None
