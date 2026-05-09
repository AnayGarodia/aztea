# SPDX-License-Identifier: Apache-2.0
"""
Tests that exercise hosted-mode routing.

These tests configure a fake hosted aztea.ai by monkey-patching
`core.hosted_client.requests` and asserting the right paths are hit. They
do not require a real network call.

The matching OSS-mode tests live in `test_oss_mode_isolation.py`.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core import auth  # noqa: E402
from core import disputes  # noqa: E402
from core import hosted_client  # noqa: E402
from core import jobs  # noqa: E402
from core import payments  # noqa: E402
from core import registry  # noqa: E402
from core import reputation  # noqa: E402
import server.application as server  # noqa: E402


def _close_module_conn(module) -> None:
    conn = getattr(module._local, "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(module._local, "conn")
    except AttributeError:
        pass


@pytest.fixture
def hosted_env(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-hosted-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)
    for m in modules:
        _close_module_conn(m)
        monkeypatch.setattr(m, "DB_PATH", str(db_path))
    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "azh_test_token")
    # Allow private outbound URLs so url_security accepts api.aztea.test in
    # CI sandboxes without DNS.
    monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    hosted_client.reset_hosted_client_for_tests()
    with TestClient(server.app):
        yield db_path
    for m in modules:
        _close_module_conn(m)
    for suffix in ("", "-shm", "-wal"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------------------
# HostedClient turns on when env is set
# ---------------------------------------------------------------------------


def test_hosted_client_enabled_with_env(hosted_env):
    client = hosted_client.get_hosted_client()
    assert client.is_enabled() is True


def test_hosted_client_judge_dispute_calls_hosted_endpoint(hosted_env, monkeypatch):
    """When hosted-mode is on, judge_dispute hits the hosted /v1/judges/judge."""
    captured: dict = {}

    class _FakeResponse:
        ok = True
        url = "https://api.aztea.test/v1/judges/judge"
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size=0):
            yield (
                b'{"verdict":"agent_wins","reasoning":"hosted judge",'
                b'"confidence":0.92,"model":"hosted-llm"}'
            )

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _FakeResponse()

    import core.hosted_client as hc

    monkeypatch.setattr(hc.requests, "post", _fake_post)
    client = hosted_client.get_hosted_client()
    result = client.judge_dispute({"dispute": {"side": "caller"}})

    assert result == {
        "verdict": "agent_wins",
        "reasoning": "hosted judge",
        "confidence": 0.92,
        "model": "hosted-llm",
    }
    assert captured["url"] == "https://api.aztea.test/v1/judges/judge"
    assert captured["headers"]["Authorization"] == "Bearer azh_test_token"
    assert "context" in captured["json"]


def test_hosted_client_judge_failure_returns_none(hosted_env, monkeypatch):
    """A 5xx from hosted falls through to None — caller will use local."""

    class _Failing:
        ok = False
        status_code = 502
        url = "https://api.aztea.test/v1/judges/judge"
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size=0):
            yield b"upstream error"

    import core.hosted_client as hc

    monkeypatch.setattr(hc.requests, "post", lambda url, **kw: _Failing())
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({"dispute": {"side": "caller"}}) is None


def test_hosted_client_publish_listing_targets_correct_path(hosted_env, monkeypatch):
    captured: dict = {}

    class _FakeResponse:
        ok = True
        url = "https://api.aztea.test/v1/registry/publish"
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size=0):
            yield (
                b'{"listing_id":"lst_abc","public_url":"https://aztea.ai/agents/x"}'
            )

    def _fake_post(url, **kwargs):
        captured["url"] = url
        return _FakeResponse()

    import core.hosted_client as hc

    monkeypatch.setattr(hc.requests, "post", _fake_post)
    client = hosted_client.get_hosted_client()
    result = client.publish_listing({"name": "test"})
    assert captured["url"] == "https://api.aztea.test/v1/registry/publish"
    assert result == {
        "listing_id": "lst_abc",
        "public_url": "https://aztea.ai/agents/x",
    }
