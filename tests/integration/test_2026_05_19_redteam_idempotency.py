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


# ===========================================================================
# Phase 4 (2026-05-19): idempotency response_body must be redacted at
# storage. The 24h DB-visible copy of a response must not contain
# sensitive field names — even when the wire response delivered them.
# ===========================================================================


def test_idempotency_response_body_redacts_sensitive_fields(client):
    """A response body persisted under an idempotency_key has its
    callback_secret / join_token / signed_payload_b64 fields stripped
    before INSERT — verified by direct DB read."""
    from core import db as _db
    from core import idempotency as _idempotency

    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"phase4 redact {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["phase4"],
    )

    # Submit hire_batch with idempotency_key + a callback_secret that
    # the response builder echoes (the F2 fix removed callback_secret
    # from JobResponse, but the redaction layer should still strip
    # ANY sensitive substring that future drift might re-introduce).
    idem_key = f"phase4-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "idempotency_key": idem_key,
            "jobs": [
                {
                    "agent_id": agent_id,
                    "input_payload": {"task": "x"},
                    "callback_url": "https://example.com/hook",
                    "callback_secret": "shh-this-is-secret-1234",
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    # Direct DB read of the stored cache row.
    with _db.get_db_connection(_idempotency.DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT response_body FROM idempotency_requests
            WHERE idempotency_key = %s AND scope = 'hire_batch'
            """,
            (idem_key,),
        ).fetchone()
    assert row is not None, "Idempotency row must exist after the call"
    stored_blob = str(row["response_body"] or "")
    # The redactor replaces values with "<redacted>" and never echoes
    # the original secret material.
    assert "shh-this-is-secret-1234" not in stored_blob, (
        "callback_secret leaked into idempotency cache!"
    )
    # And the canonical sensitive field NAMES (when present as keys)
    # have their values redacted.
    for sensitive_marker in (
        "callback_secret",
        "join_token",
        "signed_payload_b64",
        "raw_api_key",
        "private_key",
    ):
        if sensitive_marker in stored_blob:
            # If the key name appears, the value must be the redaction sentinel.
            assert "<redacted>" in stored_blob, (
                f"Cache contains key {sensitive_marker!r} but no redaction sentinel"
            )


def test_idempotency_replay_serves_redacted_body(client):
    """A second submission with the same idempotency_key returns the
    REDACTED body. This is intentional — the first response delivered
    any secret material out-of-band; replays don't need to re-emit it."""
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"phase4 replay {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["phase4r"],
    )
    idem_key = f"phase4-replay-{uuid.uuid4().hex[:8]}"
    body = {
        "idempotency_key": idem_key,
        "jobs": [
            {
                "agent_id": agent_id,
                "input_payload": {"task": "y"},
                "callback_url": "https://example.com/hook",
                "callback_secret": "another-secret-9999",
            },
        ],
    }
    first = client.post("/jobs/batch",
                        headers=_auth_headers(caller["raw_api_key"]), json=body)
    assert first.status_code == 201
    second = client.post("/jobs/batch",
                         headers=_auth_headers(caller["raw_api_key"]), json=body)
    assert second.status_code == 200, second.text
    assert second.json().get("idempotent_replay") is True
    assert "another-secret-9999" not in second.text


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
    assert resp.status_code == 503, (
        f"Suspended agent must not accept calls. Got {resp.status_code}: {resp.text}"
    )
    assert resp.json().get("error") == "agent.suspended"
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


# ===========================================================================
# F8 — budget_cents round-trips into JobResponse so callers can verify
# their submitted cap was applied.
# ===========================================================================


def test_f8_budget_cents_round_trips_in_job_response(client):
    """A single /jobs POST with budget_cents must echo the value back."""
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"F8 agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["f8"],
    )

    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "x"},
            "budget_cents": 10,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Pre-fix this field was dropped — extra="allow" passed it as None
    # because no storage existed.
    assert body.get("budget_cents") == 10, (
        f"Expected budget_cents=10 to round-trip, got {body.get('budget_cents')!r}"
    )


def test_f8_max_price_cents_alias_round_trips(client):
    """max_price_cents alias also round-trips (collapsed into budget_cents
    via MIN at the route)."""
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"F8 alias {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["f8a"],
    )

    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "x"},
            "max_price_cents": 8,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json().get("budget_cents") == 8


# ===========================================================================
# F11 — sub-cent agent prices rejected at registration; existing >= 1c
# prices still round-trip honestly.
# ===========================================================================


# ===========================================================================
# F14 — CORS preflight from a disallowed origin returns 204 (soft reject)
# instead of 400 ("Disallowed CORS origin"). Browsers still block the
# actual fetch via same-origin policy; the soft reject keeps the
# preflight from showing up as a console hard-fail.
# ===========================================================================


def test_f14_cors_preflight_soft_rejects_disallowed_origin(client):
    resp = client.options(
        "/jobs",
        headers={
            "Origin": "https://attacker.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code == 204, (
        f"Disallowed-origin preflight must soft-reject with 204, got "
        f"{resp.status_code}: {resp.text}"
    )
    # No ACAO header — the browser will refuse the subsequent request,
    # but the preflight itself succeeded.
    assert "access-control-allow-origin" not in {
        k.lower() for k in resp.headers.keys()
    }, resp.headers


def test_f11_sub_cent_price_rejected_at_registration(client):
    """Direct external agent registration at $0.003 must 400 with a
    structured reason. Pre-fix the spec accepted it and the call charged
    1c per call — a 233% overcharge."""
    worker = _register_user()

    resp = client.post(
        "/registry/register",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "name": f"F11 sub-cent {uuid.uuid4().hex[:6]}",
            "description": "should not be allowed",
            "endpoint_url": "https://example.com/sub-cent",
            "price_per_call_usd": 0.003,
            "tags": ["test"],
        },
    )
    assert resp.status_code in (400, 422), resp.text
    text = resp.text.lower()
    assert "sub-cent" in text or "at least 0.01" in text or "0.01" in text, resp.text


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
