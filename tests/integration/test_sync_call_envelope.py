from __future__ import annotations

import json
import uuid

from core import jobs
from core import payments
import server.application as server

from tests.integration.helpers import (
    _auth_headers,
    _fund_user_wallet,
    _register_agent_via_api,
    _register_user,
)


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.content = json.dumps(payload).encode("utf-8")
        self._payload = payload

    def json(self) -> dict:
        return dict(self._payload)


def test_remote_sync_call_returns_job_envelope_and_replays_idempotently(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    wallet = _fund_user_wallet(caller, 500)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Remote Sync Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["sync-envelope"],
    )

    upstream_calls: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        del url, headers, timeout, allow_redirects
        upstream_calls.append(dict(json or {}))
        return _FakeResponse({"answer": "ok", "echo": dict(json or {})})

    monkeypatch.setattr(server.http, "post", fake_post)

    auth_headers = {
        **_auth_headers(caller["raw_api_key"]),
        "X-Idempotency-Key": "same-call",
    }
    first = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=auth_headers,
        json={"task": "verify envelope"},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == "complete"
    assert first_body["cached"] is False
    assert isinstance(first_body.get("job_id"), str) and first_body["job_id"]
    assert first_body["output"]["answer"] == "ok"

    job = jobs.get_job(first_body["job_id"])
    assert job is not None
    assert job["status"] == "complete"
    assert job["output_payload"]["answer"] == "ok"

    balance_after_first = payments.get_wallet(wallet["wallet_id"])["balance_cents"]
    second = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=auth_headers,
        json={"task": "verify envelope"},
    )
    assert second.status_code == 200, second.text
    assert second.headers.get("X-Idempotency-Replayed") == "true"
    assert second.json() == first_body
    assert payments.get_wallet(wallet["wallet_id"])["balance_cents"] == balance_after_first
    assert len(upstream_calls) == 1
