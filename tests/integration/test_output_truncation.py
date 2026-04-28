from __future__ import annotations

import json
import uuid

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


def test_sync_call_summary_mode_truncates_and_full_route_returns_original(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Truncation Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["truncation"],
    )

    original_payload = {
        "summary": "A" * 6000,
        "items": list(range(80)),
    }

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        del url, json, headers, timeout, allow_redirects
        return _FakeResponse(original_payload)

    monkeypatch.setattr(server.http, "post", fake_post)

    response = client.post(
        f"/registry/agents/{agent_id}/call?mode=summary",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"task": "return large payload"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["output_truncated"] is True
    assert body["full_output_available"] is True
    assert body["full_output_path"] == f"/jobs/{body['job_id']}/full"
    assert body["output"]["summary"] != original_payload["summary"]
    assert body["output"]["items"][-1] == {"_truncated_items": 30}

    full = client.get(
        f"/jobs/{body['job_id']}/full",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert full.status_code == 200, full.text
    assert full.json()["output_payload"] == original_payload
