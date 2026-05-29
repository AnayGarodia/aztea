# SPDX-License-Identifier: Apache-2.0
"""Tests for core/deferred.py — bounded queue + daemon worker.

Covers: ordering, drop-on-overflow, head-of-line caller-drop, exception
isolation in the worker, flush-on-shutdown via sentinel.
"""
from __future__ import annotations

import threading
import time

import pytest

from core import deferred


@pytest.fixture(autouse=True)
def _reset_deferred() -> None:
    """Each test gets a clean queue + worker."""
    deferred._reset_for_tests()
    yield
    # Flush whatever may still be queued so subsequent tests start clean.
    try:
        deferred.flush(timeout=1.0)
    finally:
        deferred._reset_for_tests()


def test_enqueue_and_process_in_order() -> None:
    """The worker drains items in FIFO order and runs each handler."""
    deferred.start()
    seen: list[int] = []
    barrier = threading.Event()

    def handler(idx: int) -> None:
        seen.append(idx)
        if idx == 4:
            barrier.set()

    for i in range(5):
        assert deferred.enqueue("test_order", handler, i) is True

    assert barrier.wait(timeout=5.0), "worker did not drain all items"
    assert seen == [0, 1, 2, 3, 4]


def test_worker_exception_isolation() -> None:
    """A handler that raises does not crash the worker — next item still runs."""
    deferred.start()
    survived: list[int] = []
    done = threading.Event()

    def maybe_raise(idx: int) -> None:
        if idx == 0:
            raise RuntimeError("boom")
        survived.append(idx)
        if idx == 2:
            done.set()

    deferred.enqueue("isolation", maybe_raise, 0)
    deferred.enqueue("isolation", maybe_raise, 1)
    deferred.enqueue("isolation", maybe_raise, 2)

    assert done.wait(timeout=5.0)
    assert survived == [1, 2]


def test_flush_drains_via_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """flush() with a clean queue returns 0 in-flight items."""
    monkeypatch.setenv("AZTEA_DEFERRED_QUEUE_MAXSIZE", "32")
    deferred.start()
    seen: list[int] = []

    def handler(idx: int) -> None:
        seen.append(idx)

    for i in range(5):
        deferred.enqueue("flush", handler, i)
    # Give the worker a moment to drain.
    time.sleep(0.1)
    remaining = deferred.flush(timeout=2.0)
    assert remaining == 0
    assert sorted(seen) == [0, 1, 2, 3, 4]


def test_overflow_drops_oldest_when_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the queue is full and one caller dominates, head-of-line drop fires.

    The dominant caller is the only one represented in the queue, so a new
    enqueue from a DIFFERENT caller should evict one of theirs to make room.
    """
    monkeypatch.setenv("AZTEA_DEFERRED_QUEUE_MAXSIZE", "8")
    # Do NOT start the worker — we want the queue to remain full so we can
    # observe drop behavior.

    def noop(_idx: int) -> None:
        pass

    # Fill the queue with caller A.
    for i in range(8):
        ok = deferred.enqueue("hot", noop, i, caller_owner_id="caller_a")
        assert ok, f"enqueue {i} should have succeeded (cap=8)"

    # 9th from caller A is rejected (head-of-line refuses to penalize the same caller).
    assert deferred.enqueue("hot", noop, 99, caller_owner_id="caller_a") is False

    # But caller B's entry succeeds via head-of-line drop on caller A.
    assert deferred.enqueue("hot", noop, 100, caller_owner_id="caller_b") is True
