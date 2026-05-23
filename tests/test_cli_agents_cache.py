"""Unit tests for ``aztea.cli._agents_cache`` — TTL cache + prewarm.

Covers fresh-vs-stale detection, the get_or_fetch fast/slow paths,
and that prewarm failure leaves the cache empty (rather than
poisoning it with a partial result).
"""
from __future__ import annotations

import time

import pytest

from aztea.cli import _agents_cache as ac


@pytest.fixture(autouse=True)
def _reset_cache():
    ac.clear()
    yield
    ac.clear()


class _FakeClient:
    def __init__(self, *, payload: list, raise_exc: Exception | None = None):
        self._payload = payload
        self._raise_exc = raise_exc
        self.call_count = 0

    def list_agents(self) -> list:
        self.call_count += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._payload


def test_get_cached_empty_when_nothing_stored() -> None:
    assert ac.get_cached() is None
    assert ac.fresh() is False


def test_store_makes_cache_fresh_and_readable() -> None:
    ac.store([{"slug": "x"}])
    assert ac.fresh() is True
    assert ac.get_cached() == [{"slug": "x"}]


def test_fresh_flips_to_false_after_ttl_expires(monkeypatch) -> None:
    ac.store([{"slug": "x"}])
    # Fast-forward past the TTL.
    fake_now = time.monotonic() + ac._AGENTS_TTL_S + 1
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    assert ac.fresh() is False
    assert ac.get_cached() is None


def test_get_or_fetch_hits_network_on_miss_and_caches() -> None:
    client = _FakeClient(payload=[{"slug": "a"}, {"slug": "b"}])
    result = ac.get_or_fetch(client)
    assert result == [{"slug": "a"}, {"slug": "b"}]
    assert client.call_count == 1
    # Subsequent call within TTL must come from cache, not the network.
    again = ac.get_or_fetch(client)
    assert again == [{"slug": "a"}, {"slug": "b"}]
    assert client.call_count == 1


def test_get_or_fetch_re_hits_network_after_ttl(monkeypatch) -> None:
    client = _FakeClient(payload=[{"slug": "a"}])
    ac.get_or_fetch(client)
    assert client.call_count == 1
    # Expire the cache.
    fake_now = time.monotonic() + ac._AGENTS_TTL_S + 1
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    ac.get_or_fetch(client)
    assert client.call_count == 2


def test_prewarm_worker_failure_leaves_cache_empty(monkeypatch) -> None:
    """A failed prewarm must NOT poison the cache."""
    boom = RuntimeError("network down")

    class _FailingClient:
        def list_agents(self):
            raise boom

    # Stub the AzteaClient factory used inside _prewarm_worker.
    monkeypatch.setattr(
        "aztea.client.AzteaClient",
        lambda **kwargs: _FailingClient(),
    )
    monkeypatch.setattr("aztea.config.load_config", lambda: {"base_url": "https://x"})

    ac._prewarm_worker()  # call the worker directly — no thread, no flakiness.

    assert ac.fresh() is False
    assert ac.get_cached() is None


def test_prewarm_worker_populates_cache_on_success(monkeypatch) -> None:
    class _OkClient:
        def list_agents(self):
            return [{"slug": "ok"}]

    monkeypatch.setattr("aztea.client.AzteaClient", lambda **kwargs: _OkClient())
    monkeypatch.setattr("aztea.config.load_config", lambda: {"base_url": "https://x"})

    ac._prewarm_worker()

    assert ac.fresh() is True
    assert ac.get_cached() == [{"slug": "ok"}]
