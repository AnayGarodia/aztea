from __future__ import annotations

import json
import os
import uuid

import requests

from core import disputes
from core import jobs
from core import judges
from core import payments
import server.application as server

from tests.integration.helpers import (
    _auth_headers,
    _fund_user_wallet,
    _register_agent_via_api,
    _register_user,
)


def _assert_reconciled() -> None:
    summary = payments.compute_ledger_invariants()
    assert summary["invariant_ok"] is True, summary


def test_compare_selection_path_stays_reconciled(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    agent_a = _register_agent_via_api(client, worker["raw_api_key"], name=f"Compare A {uuid.uuid4().hex[:6]}", price=0.10)
    agent_b = _register_agent_via_api(client, worker["raw_api_key"], name=f"Compare B {uuid.uuid4().hex[:6]}", price=0.10)

    created = client.post(
        "/jobs/compare",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_ids": [agent_a, agent_b], "input_payload": {"task": "review this"}, "max_attempts": 1},
    )
    assert created.status_code == 201, created.text
    job_ids = created.json()["job_ids"]
    _assert_reconciled()

    for index, job_id in enumerate(job_ids):
        completed = jobs.update_job_status(
            job_id,
            "complete",
            output_payload={"answer": f"result-{index}"},
            completed=True,
        )
        assert completed is not None
        assert jobs.initialize_output_verification_state(job_id) is not None

    selected = client.post(
        f"/jobs/compare/{created.json()['compare_id']}/select",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"winner_agent_id": agent_a},
    )
    assert selected.status_code == 200, selected.text
    _assert_reconciled()


def test_cache_hit_path_stays_reconciled(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    agent_id = _register_agent_via_api(client, worker["raw_api_key"], name=f"Cache Agent {uuid.uuid4().hex[:6]}", price=0.10)
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
    assert first.json()["cached"] is False
    _assert_reconciled()

    second = client.post(
        f"/registry/agents/{agent_id}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"task": "same input", "use_cache": True, "cache_ttl_hours": 24},
    )
    assert second.status_code == 200, second.text
    assert second.json()["cached"] is True
    assert call_counter["count"] == 1
    _assert_reconciled()


def test_dispute_resolution_path_stays_reconciled(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    agent_id = _register_agent_via_api(client, worker["raw_api_key"], name=f"Dispute Agent {uuid.uuid4().hex[:6]}", price=0.10)

    created = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "analyze"}, "max_attempts": 2},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    complete = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim.json()["claim_token"]},
    )
    assert complete.status_code == 200, complete.text

    filed = client.post(
        f"/jobs/{job_id}/dispute",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"reason": "Output is incomplete", "evidence": "missing section"},
    )
    assert filed.status_code == 201, filed.text
    dispute_id = filed.json()["dispute_id"]
    _assert_reconciled()

    def _consensus(dispute_id_arg: str) -> dict:
        disputes.record_judgment(
            dispute_id_arg,
            judge_kind="llm_primary",
            verdict="caller_wins",
            reasoning="Caller evidence is stronger.",
            model="m1",
        )
        disputes.record_judgment(
            dispute_id_arg,
            judge_kind="llm_secondary",
            verdict="caller_wins",
            reasoning="Caller evidence is stronger.",
            model="m2",
        )
        disputes.set_dispute_consensus(dispute_id_arg, "caller_wins")
        return {"status": "consensus", "outcome": "caller_wins", "judgments": disputes.get_judgments(dispute_id_arg)}

    monkeypatch.setattr(judges, "run_judgment", _consensus)
    judged = client.post(
        f"/ops/disputes/{dispute_id}/judge",
        headers=_auth_headers(os.environ.get("API_KEY", "test-master-key")),
    )
    assert judged.status_code == 200, judged.text
    _assert_reconciled()
