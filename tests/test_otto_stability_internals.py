"""Regression tests for the stability-hardening internals that the outage effort targets.

These lock in the branches that have no other coverage (flagged by the 2026-06-28 /review):
  - core/db.py bounded-acquire FAIL-FAST (the anti-hang guard — the 2026-06-27 outage class)
  - part_000 background-worker leader lock FAIL-CLOSED under multi-worker (no split-brain /
    leaderless fleet) and fail-OPEN under a single worker
  - part_018 shared httpx client sticky-closed + idempotent close (no orphan sockets on recycle)
  - part_017 composio daily-cap FAIL-OPEN (transient lock quiet, persistent error logged) + the
    cap → 429 path

All env via monkeypatch only (never module-level os.environ.setdefault — it leaks into the
make oss-check subprocess + surface auth-matrix tests).
"""

import logging
import sqlite3
import threading
import time

import httpx
import pytest


# ── core/db.py: bounded-acquire fail-fast (anti-hang) ────────────────────────────
def test_db_pool_acquire_fails_fast_instead_of_hanging(monkeypatch):
    """An exhausted pool must raise within the acquire timeout, NOT block forever.

    A regression to an unbounded `_conn_semaphore.acquire()` re-introduces the exact
    "every route hung until restart" outage. Shrink the pool to 1, hold the only permit,
    and assert a second open fails fast.
    """
    import core.db as db

    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(db, "_conn_semaphore", sem)
    monkeypatch.setattr(db, "_DB_ACQUIRE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(db, "_MAX_CONNECTIONS", 1)

    assert sem.acquire() is True  # hold the only permit
    try:
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as ei:
            db._open_sqlite_connection(":memory:")
        elapsed = time.monotonic() - start
        assert "pool exhausted" in str(ei.value).lower()
        assert elapsed < 2.0, f"acquire blocked {elapsed:.2f}s — fail-fast guard regressed"
    finally:
        sem.release()
    # The failed acquire must not have leaked a permit (BoundedSemaphore would raise on
    # over-release); prove a fresh acquire still works.
    assert sem.acquire(timeout=1.0) is True
    sem.release()


# ── part_000: background-worker leader lock fail-closed/open ──────────────────────
def test_leader_lock_fails_closed_under_multiworker(monkeypatch):
    """A lock-SETUP failure under multi-worker must ABORT (raise), never silently fail open
    (split-brain duplicate workers) or leave a leaderless fleet."""
    import server.application as app

    monkeypatch.setenv("AZTEA_MULTI_WORKER", "1")
    monkeypatch.setattr(app, "fcntl", None, raising=False)
    monkeypatch.setattr(app, "_background_worker_lock_handle", None, raising=False)
    with pytest.raises(RuntimeError, match="multi-worker"):
        app._acquire_background_worker_lock()


def test_leader_lock_fails_open_under_single_worker(monkeypatch):
    """The same setup failure under a single worker fails OPEN (returns True) so background
    tasks still run in dev / single-process."""
    import server.application as app

    monkeypatch.delenv("AZTEA_MULTI_WORKER", raising=False)
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setattr(app, "fcntl", None, raising=False)
    monkeypatch.setattr(app, "_background_worker_lock_handle", None, raising=False)
    assert app._acquire_background_worker_lock() is True


# ── part_018: shared httpx client lifecycle ──────────────────────────────────────
@pytest.mark.asyncio
async def test_otto_http_client_sticky_closed_and_idempotent(monkeypatch):
    """After shutdown close, _otto_http() must NOT re-create an orphan client (FD leak); it
    surfaces as unavailable. A second close must be idempotent."""
    import server.application as app

    # monkeypatch restores these to (None, False) on teardown regardless of what the close
    # mutates in between, so the sticky flag can't leak into other tests.
    monkeypatch.setattr(app, "_OTTO_HTTP_CLIENT", None, raising=False)
    monkeypatch.setattr(app, "_OTTO_HTTP_CLOSED", False, raising=False)

    client = app._otto_http()
    assert client is not None
    assert app._otto_http() is client  # reused, not rebuilt

    await app._otto_http_close()
    with pytest.raises(RuntimeError, match="closed"):
        app._otto_http()
    await app._otto_http_close()  # idempotent — must not raise


# ── part_017: composio daily-cap fail-open ───────────────────────────────────────
def _capture_error_records(logger):
    """Attach a recording handler straight to the logger object — robust even if the app's
    structured logger has propagate=False (caplog's root handler would then miss it)."""
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture(level=logging.ERROR)
    logger.addHandler(handler)
    return records, handler


def test_cap_failopen_transient_lock_is_quiet():
    """A contended counter (`database is locked/busy`) fails OPEN quietly — a soft abuse cap
    must never turn a legit call into a 500, and the lock noise must not spam ERROR logs."""
    import server.application as app

    records, handler = _capture_error_records(app._otto_composio_log)
    try:
        assert app._otto_composio_cap_failopen(sqlite3.OperationalError("database is locked")) is True
        assert app._otto_composio_cap_failopen(sqlite3.OperationalError("the database is busy")) is True
        assert [r for r in records if r.levelno >= logging.ERROR] == []
    finally:
        app._otto_composio_log.removeHandler(handler)


def test_cap_failopen_persistent_error_is_logged():
    """A persistent cap-db error (corrupt/unwritable/disk-full) still fails OPEN but logs at
    ERROR so ops notices the cap is effectively disabled."""
    import server.application as app

    records, handler = _capture_error_records(app._otto_composio_log)
    try:
        assert app._otto_composio_cap_failopen(sqlite3.OperationalError("disk I/O error")) is True
        assert any("unhealthy" in r.getMessage() for r in records)
    finally:
        app._otto_composio_log.removeHandler(handler)


@pytest.mark.asyncio
async def test_composio_daily_cap_returns_429_when_exhausted(monkeypatch, tmp_path):
    """With the cap set to 1, the 2nd forwarded call gets a graceful 429 (not a crash)."""
    for _k, _v in {
        "API_KEY": "test-master-key",
        "SECRET_KEY": "dummy",
        "JWT_SECRET": "dummy",
        "DATABASE_URL": "sqlite:////tmp/otto-composio-cap-test.db",
        "ENVIRONMENT": "test",
        "TESTING": "1",
        "OTTO_APP_TOKEN": "testtoken",
        "COMPOSIO_API_KEY": "testkey",
        "OTTO_BUDGET_DB": str(tmp_path / "budget.sqlite3"),
        "OTTO_COMPOSIO_DAILY_CAP": "1",
        "AZTEA_LIMITER_DISABLED": "1",
    }.items():
        monkeypatch.setenv(_k, _v)

    import server.application as app_module

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

        text = "{}"

    class _FastClient:
        async def get(self, *a, **k):
            return _Resp()

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(app_module, "_otto_http", lambda: _FastClient())

    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer testtoken"}
        first = await client.get("/otto/composio/tools", headers=headers)
        second = await client.get("/otto/composio/tools", headers=headers)
        assert first.status_code == 200
        assert second.status_code == 429


@pytest.mark.asyncio
async def test_responses_non_json_200_returns_502_not_500(monkeypatch):
    """A 200 from LiteLLM with a non-JSON body must degrade to a clean 502, not an unhandled
    500. This is the observed 2026-06-27 prod crash: a bare gw.json() on a non-JSON 200
    (empty / truncated / HTML error page / SSE) raised JSONDecodeError → 500 to the user."""
    for _k, _v in {
        "API_KEY": "test-master-key",
        "SECRET_KEY": "dummy",
        "JWT_SECRET": "dummy",
        "DATABASE_URL": "sqlite:////tmp/otto-resp-nonjson-test.db",
        "ENVIRONMENT": "test",
        "TESTING": "1",
        "OTTO_APP_TOKEN": "testtoken",
        "OTTO_RESPONSES_USE_LITELLM": "1",
        "OTTO_RESPONSES_LITELLM_URL": "http://gw.local",
        "OTTO_RESPONSES_LITELLM_KEY": "sk-test",
        "AZTEA_LIMITER_DISABLED": "1",
    }.items():
        monkeypatch.setenv(_k, _v)

    import server.application as app_module

    class _BadJSON200:
        status_code = 200
        text = "<html>upstream hiccup</html>"

        def json(self):
            raise ValueError("not json")

    class _Client:
        async def post(self, *a, **k):
            return _BadJSON200()

    monkeypatch.setattr(app_module, "_otto_http", lambda: _Client())

    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "x"}]}]}
        headers = {"Authorization": "Bearer testtoken"}
        r = await client.post("/otto/responses", json=body, headers=headers)
        assert r.status_code == 502, f"expected clean 502, got {r.status_code}: {r.text[:200]}"
