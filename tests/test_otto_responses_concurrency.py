"""Regression guard for the 2026-06-27 outage.

The old /otto/responses was a SYNC handler that blocked an anyio threadpool slot for the
whole upstream round-trip. Under a burst of slow upstream calls the pool drained and EVERY
sync route — /health, /otto, ... — hung until the process was restarted.

The handler is now async + concurrency-bounded, so a slow upstream must NOT block other
routes. This test fires a burst of slow /otto/responses calls and asserts /health stays
responsive while they're in flight.

All env is set via monkeypatch (function-scoped, auto-reverted) and the app is imported
INSIDE the test — nothing mutates the session env at collection time, so this file can't
leak state into other tests or into the `make oss-check` subprocess.
"""

import asyncio
import time

import httpx
import pytest
from starlette.responses import JSONResponse


@pytest.mark.asyncio
async def test_slow_responses_do_not_starve_health(monkeypatch):
    # Required for a standalone import of server.application; reverted after the test so it
    # never leaks into the session env (or into subprocesses spawned by later tests).
    for _k, _v in {
        "API_KEY": "test-master-key",
        "SECRET_KEY": "dummy",
        "JWT_SECRET": "dummy",
        "DATABASE_URL": "sqlite:////tmp/otto-conc-test.db",
        "ENVIRONMENT": "test",
        "TESTING": "1",
        "OTTO_APP_TOKEN": "testtoken",
        "OTTO_RESPONSES_USE_LITELLM": "1",
    }.items():
        monkeypatch.setenv(_k, _v)

    import server.application as app_module

    async def slow_upstream(body):
        await asyncio.sleep(1.0)
        return JSONResponse(status_code=200, content={"ok": True})

    # Replace the real gateway call with a slow stub so we exercise concurrency, not Azure.
    monkeypatch.setattr(app_module, "_otto_resp_via_litellm", slow_upstream)

    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "x"}]}]}
        headers = {"Authorization": "Bearer testtoken"}

        # 40 concurrent slow upstream calls (would have starved the old ~40-thread pool).
        inflight = [
            asyncio.create_task(client.post("/otto/responses", json=body, headers=headers))
            for _ in range(40)
        ]
        await asyncio.sleep(0.25)  # let them park on the 1s upstream sleep

        start = time.perf_counter()
        health = await client.get("/health")
        elapsed = time.perf_counter() - start

        assert health.status_code == 200
        assert elapsed < 0.5, f"/health blocked {elapsed:.2f}s while responses were in flight"

        results = await asyncio.gather(*inflight)
        assert all(r.status_code == 200 for r in results)
