# SPDX-License-Identifier: Apache-2.0
"""Tests for the per-replica daily cap on the unauthenticated /public/docs/ask LLM endpoint.

# OWNS: regression coverage for the financial-DoS gate added 2026-05-26 in
#       response to CSO finding #4 — the per-IP rate limit was in place
#       (20/minute via slowapi) but no per-server daily ceiling existed, so
#       an attacker distributing across many IPs could burn through the
#       upstream LLM provider account budget.
# INVARIANTS: cap must be enforced atomically (no double-counting across
#       concurrent requests); reset must happen at UTC date rollover.

The gate lives in server/application_parts/part_011.py as
``_public_docs_ask_check_and_increment``. Tests here exercise the helper
directly so they stay fast (no LLM call, no HTTP roundtrip) and so that
the helper can be re-used later if a similar gate is added to other
unauthenticated LLM-backed endpoints.
"""

from __future__ import annotations

import os
import threading

import pytest

import server.application as _server_app  # noqa: F401 — populates module globals


def _reset_counter() -> None:
    """Wipe the in-process counter between tests."""
    state = _server_app._PUBLIC_DOCS_ASK_COUNTER_STATE
    with _server_app._PUBLIC_DOCS_ASK_COUNTER_LOCK:
        state["date"] = None
        state["count"] = 0


@pytest.fixture(autouse=True)
def _isolate_counter(monkeypatch):
    """Each test starts from a fresh counter + restored env var."""
    _reset_counter()
    monkeypatch.delenv("AZTEA_PUBLIC_DOCS_ASK_DAILY_CAP", raising=False)
    yield
    _reset_counter()


def test_default_cap_is_5000_when_env_unset():
    assert _server_app._public_docs_ask_daily_cap() == 5000


def test_env_override_takes_effect(monkeypatch):
    monkeypatch.setenv("AZTEA_PUBLIC_DOCS_ASK_DAILY_CAP", "37")
    assert _server_app._public_docs_ask_daily_cap() == 37


@pytest.mark.parametrize("bogus", ["", "abc", "0", "-1", " "])
def test_invalid_env_falls_back_to_default(monkeypatch, bogus):
    monkeypatch.setenv("AZTEA_PUBLIC_DOCS_ASK_DAILY_CAP", bogus)
    assert _server_app._public_docs_ask_daily_cap() == 5000


def test_first_request_under_cap_is_allowed(monkeypatch):
    monkeypatch.setenv("AZTEA_PUBLIC_DOCS_ASK_DAILY_CAP", "3")
    allowed, count, cap = _server_app._public_docs_ask_check_and_increment()
    assert allowed is True
    assert count == 1
    assert cap == 3


def test_request_at_cap_is_rejected(monkeypatch):
    monkeypatch.setenv("AZTEA_PUBLIC_DOCS_ASK_DAILY_CAP", "2")
    a1 = _server_app._public_docs_ask_check_and_increment()
    a2 = _server_app._public_docs_ask_check_and_increment()
    a3 = _server_app._public_docs_ask_check_and_increment()
    assert (a1[0], a2[0], a3[0]) == (True, True, False)
    assert a1[1:] == (1, 2)
    assert a2[1:] == (2, 2)
    assert a3[1:] == (2, 2)  # post-rejection count must NOT have incremented


def test_concurrent_increments_are_atomic(monkeypatch):
    """50 threads × 10 increments each against a cap of 200 — exactly 200 must
    succeed; no over-counting (would mean count > 200) and no under-counting
    (would mean count < 200 with rejections). Catches lock-ordering bugs."""
    monkeypatch.setenv("AZTEA_PUBLIC_DOCS_ASK_DAILY_CAP", "200")
    allowed_count = 0
    allowed_lock = threading.Lock()

    def worker():
        nonlocal allowed_count
        for _ in range(10):
            allowed, _spent, _cap = _server_app._public_docs_ask_check_and_increment()
            if allowed:
                with allowed_lock:
                    allowed_count += 1

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert allowed_count == 200, (
        f"Expected exactly 200 grants under a 200-cap with 500 attempts; "
        f"got {allowed_count}. Off-by-one or lock-ordering bug."
    )
    final = _server_app._PUBLIC_DOCS_ASK_COUNTER_STATE["count"]
    assert final == 200, f"Counter should be saturated at 200; got {final}"


def test_date_rollover_resets_counter(monkeypatch):
    monkeypatch.setenv("AZTEA_PUBLIC_DOCS_ASK_DAILY_CAP", "1")
    _server_app._public_docs_ask_check_and_increment()  # consumes the cap
    # Simulate the next UTC day by stamping the counter forward.
    with _server_app._PUBLIC_DOCS_ASK_COUNTER_LOCK:
        _server_app._PUBLIC_DOCS_ASK_COUNTER_STATE["date"] = "1999-01-01"
    allowed, count, cap = _server_app._public_docs_ask_check_and_increment()
    assert allowed is True, "date mismatch must trigger reset and allow the request"
    assert count == 1
    assert cap == 1
