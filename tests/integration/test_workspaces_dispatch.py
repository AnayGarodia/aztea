"""Dispatch integration: _artifact_ref substitution + auto-write (PR 3).

These tests register a fake HTTP agent and monkeypatch ``server.http.post``
to capture exactly what payload reaches the agent. That lets us verify:

* ``_workspace_id`` is stripped from the payload before dispatch.
* ``{_artifact_ref: 'ws/name'}`` is resolved inline before dispatch.
* The agent's response is auto-written back to the workspace.
* Worker-in-run callers can read/write the run's workspace without
  the original caller's key.
"""

from __future__ import annotations

import json
import uuid

import requests

from core import payments
import server.application as server

from tests.integration.helpers import (
    _auth_headers,
    _fund_user_wallet,
    _register_agent_via_api,
    _register_user,
)


def _make_echo_agent(client, monkeypatch, *, capture: dict | None = None):
    """Register an HTTP agent and stub ``server.http.post`` so its response is
    deterministic and the incoming payload is captured.

    Returns (agent_id, capture). capture['payload'] is what the agent saw.
    """
    worker = _register_user()
    caller = _register_user()
    wallet = _fund_user_wallet(caller, 500)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Echo Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["dispatch-test"],
    )
    cap = capture if capture is not None else {}

    def fake_post(url, data=None, json=None, headers=None, timeout=None, allow_redirects=None):
        del url, headers, timeout, allow_redirects
        # Plan B Phase 1: HMAC-signed dispatch sends bytes via `data=`; the
        # legacy unsigned path still uses `json=`. Accept either.
        if data is not None:
            payload = json_module.loads(data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data)
        else:
            payload = json
        cap["payload"] = payload
        resp = requests.Response()
        resp.status_code = 200
        resp.headers["Content-Type"] = "application/json"
        # Echo the input back under "echoed" so we can assert resolution.
        resp._content = json_module.dumps({"echoed": payload}).encode("utf-8")
        return resp

    json_module = json
    monkeypatch.setattr(server.http, "post", fake_post)
    return agent_id, caller, wallet, cap


def test_workspace_id_envelope_stripped_before_agent_sees_payload(client, monkeypatch):
    agent_id, caller, _wallet, cap = _make_echo_agent(client, monkeypatch)
    ws_id = client.post(
        "/workspaces", json={}, headers=_auth_headers(caller["raw_api_key"]),
    ).json()["workspace_id"]

    r = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"task": "hi", "_workspace_id": ws_id},
    )
    assert r.status_code == 200, r.text
    # The fake agent recorded the payload it received.
    sent = cap["payload"]
    assert "_workspace_id" not in sent, "envelope must not reach the agent"
    assert sent["task"] == "hi"


def test_artifact_ref_substituted_inline_before_dispatch(client, monkeypatch):
    agent_id, caller, _wallet, cap = _make_echo_agent(client, monkeypatch)
    ws_id = client.post(
        "/workspaces", json={}, headers=_auth_headers(caller["raw_api_key"]),
    ).json()["workspace_id"]
    client.put(
        f"/workspaces/{ws_id}/artifacts/cfg",
        content=b'{"deps":["a","b"]}',
        headers={
            **_auth_headers(caller["raw_api_key"]),
            "Content-Type": "application/json",
        },
    )

    r = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "task": "process",
            "config": {"_artifact_ref": f"{ws_id}/cfg"},
        },
    )
    assert r.status_code == 200, r.text
    sent = cap["payload"]
    # The agent must see the resolved dict, not the reference marker.
    assert sent["config"] == {"deps": ["a", "b"]}
    assert "_artifact_ref" not in json.dumps(sent)


def test_unknown_artifact_ref_returns_404_does_not_charge(client, monkeypatch):
    agent_id, caller, wallet, _ = _make_echo_agent(client, monkeypatch)
    starting = payments.get_wallet(wallet["wallet_id"])["balance_cents"]
    r = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"task": "x", "data": {"_artifact_ref": "ws_doesnotexist_xxx/name"}},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "workspace.not_found"
    ending = payments.get_wallet(wallet["wallet_id"])["balance_cents"]
    assert ending == starting, "no charge should occur when resolution fails"


def test_artifact_ref_to_other_users_workspace_returns_403(client, monkeypatch):
    agent_id, caller, _wallet, _ = _make_echo_agent(client, monkeypatch)
    other = _register_user()
    other_ws = client.post(
        "/workspaces", json={}, headers=_auth_headers(other["raw_api_key"]),
    ).json()["workspace_id"]
    client.put(
        f"/workspaces/{other_ws}/artifacts/secret",
        content=b"oss",
        headers={
            **_auth_headers(other["raw_api_key"]),
            "Content-Type": "text/plain",
        },
    )
    r = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"task": "x", "data": {"_artifact_ref": f"{other_ws}/secret"}},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "workspace.forbidden"


def test_auto_writes_output_to_workspace_after_settlement(client, monkeypatch):
    agent_id, caller, _wallet, _ = _make_echo_agent(client, monkeypatch)
    ws_id = client.post(
        "/workspaces", json={}, headers=_auth_headers(caller["raw_api_key"]),
    ).json()["workspace_id"]

    r = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"task": "hi", "_workspace_id": ws_id},
    )
    assert r.status_code == 200, r.text

    listing = client.get(
        f"/workspaces/{ws_id}/artifacts",
        headers=_auth_headers(caller["raw_api_key"]),
    ).json()["artifacts"]
    output_artifacts = [a for a in listing if a["name"].startswith("outputs/")]
    assert len(output_artifacts) == 1
    # The artifact name carries the agent slug + job_id.
    assert ".json" in output_artifacts[0]["name"]


def test_invalid_workspace_id_envelope_returns_422(client, monkeypatch):
    agent_id, caller, _wallet, _ = _make_echo_agent(client, monkeypatch)
    r = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"task": "x", "_workspace_id": "not-a-real-id"},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "request.invalid_input"
