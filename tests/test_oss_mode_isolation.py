# SPDX-License-Identifier: Apache-2.0
"""
Tests that verify the OSS build runs fully self-contained.

When AZTEA_HOSTED_API_URL is unset:
  - HostedClient.is_enabled() returns False.
  - HostedClient.judge_dispute / call_agent / publish_listing / fetch_trust
    return None without touching the network.
  - HostedClient.push_rating returns False without touching the network.
  - Stripe routes return 501 with the structured "stripe_not_configured" error.
  - The /registry/agents/{id}/publish route returns 501.
  - The /registry/agents/{id}/global-trust route returns 501.

These guarantees are the load-bearing OSS contract. If any of them break,
the OSS build is leaking calls or features to aztea.ai that should have
stayed local.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

# IMPORTANT — these tests assert OSS-mode behavior. Strip any inherited
# hosted-mode env so a local dev export doesn't silently mask a regression.
for _var in (
    "AZTEA_HOSTED_API_URL",
    "AZTEA_HOSTED_API_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PUBLISHABLE_KEY",
):
    os.environ.pop(_var, None)

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


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-oss-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)
    for m in modules:
        _close_module_conn(m)
        monkeypatch.setattr(m, "DB_PATH", str(db_path))
    # Force OSS-mode at the module level. The .env file in the dev environment
    # may populate STRIPE_SECRET_KEY / AZTEA_HOSTED_* at import time via
    # python-dotenv; we stomp those module-level constants and the env vars
    # so the test really exercises the OSS code paths.
    monkeypatch.setattr(server, "_STRIPE_SECRET_KEY", "", raising=False)
    monkeypatch.setattr(server, "_STRIPE_WEBHOOK_SECRET", "", raising=False)
    monkeypatch.setattr(server, "_STRIPE_PUBLISHABLE_KEY", "", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("AZTEA_HOSTED_API_URL", raising=False)
    monkeypatch.delenv("AZTEA_HOSTED_API_KEY", raising=False)
    hosted_client.reset_hosted_client_for_tests()
    with TestClient(server.app):
        yield
    for m in modules:
        _close_module_conn(m)
    for suffix in ("", "-shm", "-wal"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------------------
# HostedClient short-circuits without env
# ---------------------------------------------------------------------------


def test_hosted_client_disabled_when_env_unset():
    client = hosted_client.get_hosted_client()
    assert client.is_enabled() is False


def test_hosted_client_methods_return_none_or_false_when_disabled():
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({"foo": "bar"}) is None
    assert client.call_agent("web_search", {"query": "x"}) is None
    assert client.publish_listing({"name": "x"}) is None
    assert client.fetch_trust("did:web:example.com:agents:abc") is None
    # push_rating returns False on no-op (couldn't push)
    assert client.push_rating({"job_id": "x", "rating": 5}) is False


def test_hosted_client_judge_does_not_touch_network(monkeypatch):
    """Critical: a disabled client must not even *attempt* to make the
    HTTP request. We monkeypatch `requests.post` to raise loudly so any
    accidental network call surfaces as a test failure."""
    import requests

    def _explode(*args, **kwargs):
        raise AssertionError("HostedClient called requests.post in OSS-mode")

    monkeypatch.setattr(requests, "post", _explode)
    client = hosted_client.get_hosted_client()
    # All these would call requests.post on a real client; with our
    # monkeypatch, they must short-circuit before reaching it.
    assert client.judge_dispute({"foo": "bar"}) is None
    assert client.call_agent("anything", {}) is None
    assert client.publish_listing({}) is None
    assert client.push_rating({}) is False


# ---------------------------------------------------------------------------
# Stripe routes return 501 with structured payload
# ---------------------------------------------------------------------------


def _client_for_app() -> TestClient:
    return TestClient(server.app)


def test_stripe_topup_returns_501_in_oss_mode():
    client = _client_for_app()
    response = client.post(
        "/wallets/topup/session",
        json={"wallet_id": "wallet-oss-test", "amount_cents": 500},
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert response.status_code == 501
    body = response.json()
    # The framework's HTTPException handler flattens detail into the top-level
    # error envelope: {error, message, details, request_id}.
    assert body.get("error") == "payment.stripe_not_configured"
    assert "aztea.ai" in (body.get("details") or {}).get("hosted_url", "")


def test_stripe_webhook_returns_501_in_oss_mode():
    client = _client_for_app()
    response = client.post(
        "/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "fake"},
    )
    assert response.status_code == 501


def test_stripe_connect_onboard_returns_501_in_oss_mode():
    client = _client_for_app()
    response = client.post(
        "/wallets/connect/onboard",
        json={"return_url": "https://example.com/wallet", "refresh_url": "https://example.com/wallet"},
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert response.status_code == 501


# ---------------------------------------------------------------------------
# Registry publish + global trust 501 in OSS-mode
# ---------------------------------------------------------------------------


def _new_user() -> dict:
    suffix = uuid.uuid4().hex[:8]
    return auth.register_user(
        username=f"oss-{suffix}",
        email=f"oss-{suffix}@example.com",
        password="password123",
    )


def _register_agent(owner_id: str) -> str:
    return registry.register_agent(
        name=f"oss-agent-{uuid.uuid4().hex[:6]}",
        description="oss isolation test agent",
        endpoint_url=f"https://example.com/{uuid.uuid4().hex[:6]}",
        price_per_call_usd=0.05,
        tags=["oss-test"],
        owner_id=owner_id,
        embed_listing=False,
    )


def test_publish_to_public_registry_returns_501_in_oss_mode():
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    # Master key satisfies auth and ownership for this test (master bypasses scope).
    client = _client_for_app()
    response = client.post(
        f"/registry/agents/{aid}/publish",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert response.status_code == 501
    body = response.json()
    assert body.get("error") == "registry.public_publish_disabled"


def test_global_trust_returns_501_in_oss_mode():
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    client = _client_for_app()
    response = client.get(
        f"/registry/agents/{aid}/global-trust",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert response.status_code == 501
    body = response.json()
    assert body.get("error") == "registry.global_trust_disabled"
