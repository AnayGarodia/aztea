"""Finding A-C6 (2026-05-30 review): RequestBudget.used and the exhaustion-log
throttle were mutated without synchronization.

Before the fix, RequestBudget.try_consume did `self.used += 1` outside any lock.
When a handler threads one RequestBudget through call sites on different threads
(e.g. a parallel runner), concurrent try_consume calls lose updates and the
per-request cap is bypassed — more than `cap` calls succeed.

After the fix, the read-modify-write is guarded by a per-instance lock, so the
number of successful consumes never exceeds the cap under concurrency.
"""

from __future__ import annotations

import threading

from core.registry._llm_budget import RequestBudget


def test_request_budget_cap_holds_under_concurrency():
    cap = 50
    rb = RequestBudget(cap=cap)
    # Far more concurrent attempts than the cap, across many threads.
    attempts = 500
    successes = []
    successes_lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker(n):
        barrier.wait()  # maximize contention
        for _ in range(n):
            if rb.try_consume("stress"):
                with successes_lock:
                    successes.append(1)

    threads = [threading.Thread(target=worker, args=(attempts // 20,)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The invariant: never more successful consumes than the cap allows.
    assert len(successes) == cap, f"cap bypassed: {len(successes)} > {cap}"
    assert rb.used == cap


def test_request_budget_refund_does_not_underflow_under_concurrency():
    rb = RequestBudget(cap=100)
    # Consume some, then have many threads refund concurrently.
    for _ in range(40):
        assert rb.try_consume("x")
    assert rb.used == 40

    barrier = threading.Barrier(40)

    def refunder():
        barrier.wait()
        rb.refund()

    threads = [threading.Thread(target=refunder) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 40 refunds of 40 consumed → exactly 0, never negative.
    assert rb.used == 0
