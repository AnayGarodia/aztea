"""Regression: a slow /otto/composio upstream must not starve other routes.

Mirrors tests/test_otto_responses_concurrency.py. After making otto_composio async +
semaphore-bounded (part_017), a burst of slow Composio calls must NOT block the worker so
/health stays responsive. All env via monkeypatch (never module-level setdefault — that leaks
into the make oss-check subprocess + surface auth-matrix tests).
"""

import asyncio
import time

import httpx
import pytest


@pytest.mark.asyncio
async def test_slow_composio_does_not_starve_health(monkeypatch, tmp_path):
    for _k, _v in {
        "API_KEY": "test-master-key",
        "SECRET_KEY": "dummy",
        "JWT_SECRET": "dummy",
        "DATABASE_URL": "sqlite:////tmp/otto-composio-conc-test.db",
        "ENVIRONMENT": "test",
        "TESTING": "1",
        "OTTO_APP_TOKEN": "testtoken",
        "COMPOSIO_API_KEY": "testkey",
        "OTTO_BUDGET_DB": str(tmp_path / "budget.sqlite3"),
        "OTTO_COMPOSIO_DAILY_CAP": "100000",
        "AZTEA_LIMITER_DISABLED": "1",
    }.items():
        monkeypatch.setenv(_k, _v)

    import server.application as app_module

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

        text = "{}"

    class _SlowClient:
        async def get(self, *a, **k):
            await asyncio.sleep(1.0)
            return _Resp()

        async def post(self, *a, **k):
            await asyncio.sleep(1.0)
            return _Resp()

    # Replace the shared client with a slow stub so we exercise concurrency, not Composio.
    monkeypatch.setattr(app_module, "_otto_http", lambda: _SlowClient())

    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer testtoken"}
        # 40 concurrent slow Composio GETs to an allowlisted path (/tools).
        inflight = [
            asyncio.create_task(client.get("/otto/composio/tools", headers=headers))
            for _ in range(40)
        ]
        await asyncio.sleep(0.25)  # let them park on the 1s upstream sleep

        start = time.perf_counter()
        health = await client.get("/health")
        elapsed = time.perf_counter() - start

        assert health.status_code == 200
        assert elapsed < 0.5, f"/health blocked {elapsed:.2f}s while composio calls were in flight"

        results = await asyncio.gather(*inflight)
        assert all(r.status_code == 200 for r in results)
