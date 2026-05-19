"""F1 end-to-end behavior tests for hire_batch idempotency.

The red-team reproed 5 identical submissions producing 5 fresh batch_ids
plus no 409 on mismatched body. The source-anchored regression test from
the previous sprint passed because it grepped for `_idem.begin(` literals.
These tests call the real route and assert on response shape.
"""

from __future__ import annotations

import uuid

from tests.integration.support import *  # noqa: F403


def test_f1_hire_batch_idempotency_replay_returns_same_batch_id(client):
    """Same idempotency_key + identical body → 200 idempotent_replay with the same batch_id."""
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"F1 idem agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["f1-idem"],
    )

    body = {
        "idempotency_key": "priced-idem-v2",
        "jobs": [
            {"agent_id": agent_id, "input_payload": {"task": "a"}},
            {"agent_id": agent_id, "input_payload": {"task": "b"}},
        ],
    }
    first = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json=body,
    )
    assert first.status_code == 201, first.text
    first_body = first.json()
    first_batch_id = first_body["batch_id"]
    first_job_ids = sorted(j["job_id"] for j in first_body["jobs"])

    # Identical body, same key — must replay.
    second = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json=body,
    )
    assert second.status_code == 200, (
        f"Idempotency replay must return 200, got {second.status_code}: {second.text}"
    )
    second_body = second.json()
    assert second_body.get("idempotent_replay") is True, second_body
    assert second_body["batch_id"] == first_batch_id, (
        f"Replay returned a NEW batch_id {second_body['batch_id']} instead of {first_batch_id}"
    )
    second_job_ids = sorted(j["job_id"] for j in second_body["jobs"])
    assert second_job_ids == first_job_ids, (
        f"Replay returned different job_ids: {second_job_ids} vs {first_job_ids}"
    )


def test_f1_hire_batch_idempotency_mismatched_body_returns_409(client):
    """Same idempotency_key with DIFFERENT body → 409 idempotency.payload_mismatch."""
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"F1 mismatch agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["f1-mismatch"],
    )

    first = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "idempotency_key": "mismatch-k1",
            "jobs": [{"agent_id": agent_id, "input_payload": {"task": "ORIGINAL"}}],
        },
    )
    assert first.status_code == 201, first.text

    # Same key, DIFFERENT body — must NOT proceed, must return 409.
    second = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "idempotency_key": "mismatch-k1",
            "jobs": [{"agent_id": agent_id, "input_payload": {"task": "TAMPERED"}}],
        },
    )
    assert second.status_code == 409, (
        f"Mismatched body under same idempotency_key must return 409, got "
        f"{second.status_code}: {second.text}"
    )
    body = second.json()
    assert body.get("error") == "idempotency.payload_mismatch", body


def test_f1_hire_batch_no_key_does_not_dedup(client):
    """Without an idempotency_key, two identical submissions get two batch_ids."""
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"F1 nokey agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["f1-nokey"],
    )

    body = {
        "jobs": [{"agent_id": agent_id, "input_payload": {"task": "x"}}],
    }
    r1 = client.post("/jobs/batch", headers=_auth_headers(caller["raw_api_key"]), json=body)
    r2 = client.post("/jobs/batch", headers=_auth_headers(caller["raw_api_key"]), json=body)
    assert r1.status_code == 201
    assert r2.status_code == 201
    # Different batch_ids prove the dedup didn't accidentally fire.
    assert r1.json()["batch_id"] != r2.json()["batch_id"]


# ===========================================================================
# F6 — session_budget pre-flight: reject the whole batch BEFORE opening
# escrow on job 1, so the structured error code surfaces and no refunds
# need to flow.
# ===========================================================================


def test_f6_session_budget_preflight_rejects_batch_before_charge(client):
    """A batch that would push session_spent past session_budget_cents 402s
    cleanly without opening escrow on any job."""
    worker = _register_user()
    caller = _register_user()
    wallet = _fund_user_wallet(caller, 1000)
    # 10¢ cap with no prior session spend; a 12¢ batch must be rejected.
    payments.set_wallet_session_budget(wallet["wallet_id"], 10)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"F6 agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["f6"],
    )

    pre_balance = payments.get_wallet(wallet["wallet_id"])["balance_cents"]
    resp = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "jobs": [
                {"agent_id": agent_id, "input_payload": {"task": "a"}},
                {"agent_id": agent_id, "input_payload": {"task": "b"}},
            ]
        },
    )
    assert resp.status_code == 402, resp.text
    body = resp.json()
    assert body.get("error") == "wallet.session_budget_exceeded", body
    # No escrow opened — balance unchanged.
    post_balance = payments.get_wallet(wallet["wallet_id"])["balance_cents"]
    assert post_balance == pre_balance, (
        f"Balance moved despite pre-flight rejection: {pre_balance} → {post_balance}"
    )


# ===========================================================================
# F7 — suspended agents must not accept calls. Pre-fix list_agents excluded
# BOTH 'banned' and 'suspended' but the call gate only blocked 'banned'.
# ===========================================================================


def test_f7_suspended_agent_rejected_at_call_path(client):
    """A suspended agent must reject calls — the call gate must mirror
    the list-agents filter that hides 'suspended' alongside 'banned'."""
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"F7 suspend {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["f7"],
    )

    # Suspend the agent.
    registry.set_agent_status(agent_id, "suspended", reason="f7 test")

    # Call path must reject. Pre-fix this returned 201 + charged.
    pre_balance = payments.get_or_create_wallet(f"user:{caller['user_id']}")["balance_cents"]
    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "abuse"}},
    )
    assert resp.status_code in (403, 404), (
        f"Suspended agent must not accept calls. Got {resp.status_code}: {resp.text}"
    )
    # No charge applied.
    post_balance = payments.get_or_create_wallet(f"user:{caller['user_id']}")["balance_cents"]
    assert post_balance == pre_balance, (
        f"Balance moved despite suspended-agent rejection: {pre_balance} → {post_balance}"
    )

    # Same for /jobs/batch — the agent_id resolves to a hidden row so the
    # batch path either rejects via 422/404 or filters the job out
    # entirely. Either way, no escrow is opened.
    batch_resp = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"jobs": [{"agent_id": agent_id, "input_payload": {"task": "abuse"}}]},
    )
    assert batch_resp.status_code in (400, 403, 404, 422), (
        f"Suspended agent must not accept batch calls. "
        f"Got {batch_resp.status_code}: {batch_resp.text}"
    )
    # No charge applied at the batch level either.
    final_balance = payments.get_or_create_wallet(f"user:{caller['user_id']}")["balance_cents"]
    assert final_balance == pre_balance, (
        f"Balance moved after suspended-agent batch attempt: "
        f"{pre_balance} → {final_balance}"
    )


def test_f6_session_budget_allows_batch_within_cap(client):
    """A batch that fits inside session_budget_cents succeeds."""
    worker = _register_user()
    caller = _register_user()
    wallet = _fund_user_wallet(caller, 1000)
    payments.set_wallet_session_budget(wallet["wallet_id"], 20)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"F6 ok agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["f6-ok"],
    )

    resp = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "jobs": [
                {"agent_id": agent_id, "input_payload": {"task": "a"}},
                {"agent_id": agent_id, "input_payload": {"task": "b"}},
            ]
        },
    )
    assert resp.status_code == 201, resp.text
