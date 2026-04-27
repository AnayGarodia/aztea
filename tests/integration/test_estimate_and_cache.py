from __future__ import annotations

import json
import uuid

import requests

from core import payments
from core import registry
import server.application as server

from tests.integration.helpers import (
    _auth_headers,
    _fund_user_wallet,
    _register_agent_via_api,
    _register_user,
)


def test_agent_estimate_endpoint_returns_all_in_cost_and_latency(client):
    worker = _register_user()
    caller = _register_user()
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Estimate Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["estimate"],
    )
    for _ in range(5):
        registry.update_call_stats(agent_id, latency_ms=120.0, success=True, price_cents=10)

    response = client.post(
        f"/agents/{agent_id}/estimate",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"task": "estimate this"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent_id
    assert body["estimated_cost_cents"] == 11
    assert body["p50_latency_ms"] == 120
    assert body["p95_latency_ms"] == 120
    assert body["confidence"] == "medium"
    assert body["based_on_calls"] == 5


def test_registry_call_use_cache_returns_cached_output_without_second_charge(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    wallet = _fund_user_wallet(caller, 500)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Cached Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["cache"],
    )
    monkeypatch.setattr(server._cache, "_current_trust_score", lambda _agent_id: 95.0)

    call_counter = {"count": 0}

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        del url, json, headers, timeout, allow_redirects
        call_counter["count"] += 1
        resp = requests.Response()
        resp.status_code = 200
        resp.headers["Content-Type"] = "application/json"
        resp._content = json_module.dumps({"answer": "cached result"}).encode("utf-8")
        return resp

    json_module = json
    monkeypatch.setattr(server.http, "post", fake_post)

    first = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"task": "same input", "use_cache": True, "cache_ttl_hours": 24},
    )
    assert first.status_code == 200, first.text
    assert first.json()["answer"] == "cached result"
    assert call_counter["count"] == 1
    first_balance = payments.get_wallet(wallet["wallet_id"])["balance_cents"]
    assert first_balance == 489

    second = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"task": "same input", "use_cache": True, "cache_ttl_hours": 24},
    )
    assert second.status_code == 200, second.text
    assert second.json()["answer"] == "cached result"
    assert second.json()["cache_hit"] is True
    assert second.json()["cost_usd"] == 0
    assert call_counter["count"] == 1
    second_balance = payments.get_wallet(wallet["wallet_id"])["balance_cents"]
    assert second_balance == first_balance
