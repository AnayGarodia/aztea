from __future__ import annotations

import uuid

from tests.integration.helpers import (
    _auth_headers,
    _fund_user_wallet,
    _register_agent_via_api,
    _register_user,
)


def test_jobs_create_persists_client_id_from_header(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Client Tag Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["client-id"],
    )

    headers = _auth_headers(caller["raw_api_key"])
    headers["X-Aztea-Client"] = "claude-code"
    created = client.post(
        "/jobs",
        headers=headers,
        json={"agent_id": agent_id, "input_payload": {"task": "tag this"}, "max_attempts": 1},
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    assert created_body["client_id"] == "claude-code"

    fetched = client.get(f"/jobs/{created_body['job_id']}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["client_id"] == "claude-code"


def test_jobs_batch_create_allows_per_job_client_id_override(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Batch Client Tag Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["client-id"],
    )

    headers = _auth_headers(caller["raw_api_key"])
    headers["X-Aztea-Client"] = "cursor"
    created = client.post(
        "/jobs/batch",
        headers=headers,
        json={
            "jobs": [
                {
                    "agent_id": agent_id,
                    "input_payload": {"task": "first"},
                    "max_attempts": 1,
                    "client_id": "codex",
                },
                {
                    "agent_id": agent_id,
                    "input_payload": {"task": "second"},
                    "max_attempts": 1,
                },
            ]
        },
    )
    assert created.status_code == 201, created.text
    jobs = created.json()["jobs"]
    assert jobs[0]["client_id"] == "codex"
    assert jobs[1]["client_id"] == "cursor"
