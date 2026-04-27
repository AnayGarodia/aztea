from __future__ import annotations

import uuid

from core import jobs
from core import payments

from tests.integration.helpers import (
    _auth_headers,
    _fund_user_wallet,
    _register_agent_via_api,
    _register_user,
)


def test_compare_jobs_selects_winner_and_refunds_non_winner(client):
    worker = _register_user()
    caller = _register_user()
    wallet = _fund_user_wallet(caller, 500)
    agent_a = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Compare Agent A {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["compare"],
    )
    agent_b = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Compare Agent B {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["compare"],
    )

    headers = _auth_headers(caller["raw_api_key"])
    created = client.post(
        "/jobs/compare",
        headers=headers,
        json={
            "agent_ids": [agent_a, agent_b],
            "input_payload": {"task": "review this"},
            "max_attempts": 1,
        },
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    assert created_body["total_charged_cents"] == 22
    job_ids = created_body["job_ids"]
    assert len(job_ids) == 2
    assert payments.get_wallet(wallet["wallet_id"])["balance_cents"] == 478

    for index, job_id in enumerate(job_ids):
        updated = jobs.update_job_status(
            job_id,
            "complete",
            output_payload={"answer": f"result-{index}"},
            completed=True,
        )
        assert updated is not None
        initialized = jobs.initialize_output_verification_state(job_id)
        assert initialized is not None
        assert initialized["output_verification_status"] == "pending"

    status = client.get(f"/jobs/compare/{created_body['compare_id']}", headers=headers)
    assert status.status_code == 200, status.text
    status_body = status.json()
    assert status_body["status"] == "complete"
    assert status_body["selection_required"] is True

    selected = client.post(
        f"/jobs/compare/{created_body['compare_id']}/select",
        headers=headers,
        json={"winner_agent_id": agent_a},
    )
    assert selected.status_code == 200, selected.text
    selected_body = selected.json()
    assert selected_body["winner_agent_id"] == agent_a
    assert len(selected_body["refunded_job_ids"]) == 1

    winner_job = jobs.get_job(job_ids[0])
    loser_job = jobs.get_job(job_ids[1])
    assert winner_job is not None and loser_job is not None
    assert winner_job["settled_at"] is not None
    assert loser_job["settled_at"] is not None
    assert winner_job["output_verification_status"] == "accepted"
    assert loser_job["output_verification_status"] == "rejected"
    assert payments.get_wallet(wallet["wallet_id"])["balance_cents"] == 489
