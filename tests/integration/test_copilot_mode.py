"""Co-Pilot Mode integration tests.

Covers the bidirectional protocol: stop_when validation at submit, partial_output
streaming + lease behavior, steer + rate limits, race-ordering after terminal
transition, partial-unit billing settlement, and JWS receipt verification against
the agent's published JWK.

See docs/superpowers/specs/2026-05-09-copilot-mode-design.md.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from tests.integration.support import *  # noqa: F403


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _claim(client, raw_api_key: str, job_id: str) -> str:
    resp = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(raw_api_key),
        json={"lease_seconds": 120},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["claim_token"]


def _emit_partial(client, raw_api_key: str, job_id: str, payload: dict) -> dict:
    resp = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(raw_api_key),
        json={"type": "partial_output", "payload": {"payload": payload}},
    )
    return resp


def _post_steer(client, raw_api_key: str, job_id: str, message: str) -> dict:
    resp = client.post(
        f"/jobs/{job_id}/steer",
        headers=_auth_headers(raw_api_key),
        json={"message": message},
    )
    return resp


def _setup_caller_and_agent(client) -> tuple[dict, dict, str]:
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Copilot Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["copilot-mode"],
    )
    return worker, caller, agent_id


# ---------------------------------------------------------------------------
# Submit-time validation
# ---------------------------------------------------------------------------


def test_stop_when_invalid_jmespath_rejected_at_submit(client):
    _, caller, agent_id = _setup_caller_and_agent(client)
    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "x"},
            "stop_when": [{"label": "bad", "expr": "@@@invalid"}],
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    # error envelope can be flat or nested under detail; tolerate both shapes
    err = body.get("error") or body.get("detail", {}).get("error")
    assert err == "stop_when.invalid", body


def test_stop_when_complexity_rejected_at_submit(client):
    _, caller, agent_id = _setup_caller_and_agent(client)
    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "x"},
            "stop_when": [{"label": "deep", "expr": "a[*][*][*][*]"}],
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    err = body.get("error") or body.get("detail", {}).get("error")
    assert err == "stop_when.invalid", body


# ---------------------------------------------------------------------------
# Lease behavior
# ---------------------------------------------------------------------------


def test_partial_output_extends_lease(client):
    worker, caller, agent_id = _setup_caller_and_agent(client)
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    job_id = job["job_id"]
    _claim(client, worker["raw_api_key"], job_id)

    before = jobs.get_job(job_id)["lease_expires_at"]
    resp = _emit_partial(
        client, worker["raw_api_key"], job_id, {"step": "1", "note": "thinking"}
    )
    assert resp.status_code == 201, resp.text
    after = jobs.get_job(job_id)["lease_expires_at"]
    assert after > before, "partial_output should extend the lease"

    j = jobs.get_job(job_id)
    assert j["partials_count"] == 1


def test_steer_does_not_extend_lease(client):
    worker, caller, agent_id = _setup_caller_and_agent(client)
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    job_id = job["job_id"]
    _claim(client, worker["raw_api_key"], job_id)

    before = jobs.get_job(job_id)["lease_expires_at"]
    resp = _post_steer(client, caller["raw_api_key"], job_id, "use python")
    assert resp.status_code in (200, 201), resp.text
    after = jobs.get_job(job_id)["lease_expires_at"]
    assert after == before, "steer must not extend the lease"

    j = jobs.get_job(job_id)
    assert j["steer_count"] == 1


# ---------------------------------------------------------------------------
# Stop_when matching
# ---------------------------------------------------------------------------


def test_stop_when_aborts_at_exact_partial(client):
    worker, caller, agent_id = _setup_caller_and_agent(client)
    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "scan"},
            "stop_when": [
                {"label": "critical", "expr": "severity == `critical`"},
            ],
            "billing_unit": "partial",
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]
    _claim(client, worker["raw_api_key"], job_id)

    # First partial does NOT match — job stays running.
    r1 = _emit_partial(
        client, worker["raw_api_key"], job_id, {"severity": "low"}
    )
    assert r1.status_code == 201, r1.text
    assert jobs.get_job(job_id)["status"] != "stopped"

    # Second partial matches — job transitions to stopped, stop_reason recorded.
    r2 = _emit_partial(
        client, worker["raw_api_key"], job_id, {"severity": "critical"}
    )
    assert r2.status_code == 201, r2.text

    j = jobs.get_job(job_id)
    assert j["status"] == "stopped"
    assert j["partials_count"] == 2
    assert j["terminal_at"] is not None
    assert j["stop_reason_json"] is not None
    reason = json.loads(j["stop_reason_json"])
    assert reason["label"] == "critical"
    assert reason["matched_message_id"] is not None


# ---------------------------------------------------------------------------
# Race ordering after terminal
# ---------------------------------------------------------------------------


def test_partial_after_terminal_rejected(client):
    worker, caller, agent_id = _setup_caller_and_agent(client)
    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "x"},
            "stop_when": [{"label": "stop", "expr": "halt == `true`"}],
        },
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]
    _claim(client, worker["raw_api_key"], job_id)
    _emit_partial(client, worker["raw_api_key"], job_id, {"halt": True})
    assert jobs.get_job(job_id)["status"] == "stopped"

    # Post-terminal partial — should be rejected.
    r = _emit_partial(
        client, worker["raw_api_key"], job_id, {"after_terminal": True}
    )
    assert r.status_code == 409, r.text
    body = r.json()
    err = body.get("error") or body.get("detail", {}).get("error")
    assert err == "job.terminal", body


def test_steer_after_terminal_rejected(client):
    worker, caller, agent_id = _setup_caller_and_agent(client)
    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "x"},
            "stop_when": [{"label": "stop", "expr": "halt == `true`"}],
        },
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]
    _claim(client, worker["raw_api_key"], job_id)
    _emit_partial(client, worker["raw_api_key"], job_id, {"halt": True})
    assert jobs.get_job(job_id)["status"] == "stopped"

    r = _post_steer(client, caller["raw_api_key"], job_id, "too late")
    assert r.status_code == 409, r.text
    body = r.json()
    err = body.get("error") or body.get("detail", {}).get("error")
    assert err == "job.terminal", body


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def test_steer_rate_limit_per_job_429(client):
    worker, caller, agent_id = _setup_caller_and_agent(client)
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    job_id = job["job_id"]
    _claim(client, worker["raw_api_key"], job_id)

    # Cap is 20 per job. Drive past it.
    last_status = None
    for i in range(25):
        r = _post_steer(client, caller["raw_api_key"], job_id, f"steer-{i}")
        last_status = r.status_code
        if last_status == 429:
            body = r.json()
            err = body.get("error") or body.get("detail", {}).get("error")
            assert err in {
                "steer.rate_limit.per_job",
                "steer.rate_limit.per_caller",
            }, body
            break
    else:
        pytest.fail(f"never hit 429; last status {last_status}")


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def test_receipt_built_and_signature_verifies(client):
    worker, caller, agent_id = _setup_caller_and_agent(client)
    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "scan"},
            "stop_when": [{"label": "stop", "expr": "halt == `true`"}],
            "billing_unit": "call",
        },
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]
    _claim(client, worker["raw_api_key"], job_id)
    _emit_partial(client, worker["raw_api_key"], job_id, {"halt": True})
    assert jobs.get_job(job_id)["status"] == "stopped"

    # Receipt route — runner should have built it on the sync drain.
    rr = client.get(
        f"/jobs/{job_id}/receipt",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert rr.status_code == 200, rr.text
    body = rr.json()
    assert "jws" in body and "transcript" in body and "public_jwk" in body
    jws: str = body["jws"]
    parts = jws.split(".")
    assert len(parts) == 3, "jws must have 3 base64url segments"

    header_bytes = _b64url_decode(parts[0])
    header = json.loads(header_bytes.decode("utf-8"))
    assert header.get("alg") == "EdDSA"

    # Reconstruct signing input and verify with the published JWK.
    signing_input = (parts[0] + "." + parts[1]).encode("ascii")
    signature = _b64url_decode(parts[2])

    jwk = body["public_jwk"]
    assert jwk.get("kty") == "OKP" and jwk.get("crv") == "Ed25519"
    public_bytes = _b64url_decode(jwk["x"])
    pub = ed25519.Ed25519PublicKey.from_public_bytes(public_bytes)
    pub.verify(signature, signing_input)  # raises on mismatch

    transcript = body["transcript"]
    assert transcript["job_id"] == job_id
    assert transcript["terminal_state"] == "stopped"
    assert transcript["stop_reason"]["label"] == "stop"
    # Messages must be id-ordered and include the stop-firing partial.
    msg_ids = [m.get("id") or m.get("message_id") for m in transcript["messages"]]
    assert msg_ids == sorted(msg_ids)
    assert any(m["type"] == "partial_output" for m in transcript["messages"])


# ---------------------------------------------------------------------------
# Partial-unit settlement
# ---------------------------------------------------------------------------


def test_billing_unit_partial_settles_proportionally(client):
    """When billing_unit='partial' and a job stops at partial N, agent earns
    proportional units and the rest is refunded.

    Without a declared max_units, the runner uses partials_count both as
    numerator and denominator — so a 1-partial stop gets the full price.
    The interesting check is the case where multiple partials precede the
    stop: agent gets all the price (units==total), no refund.
    """
    worker, caller, agent_id = _setup_caller_and_agent(client)
    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "scan"},
            "stop_when": [{"label": "stop", "expr": "severity == `critical`"}],
            "billing_unit": "partial",
        },
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]
    _claim(client, worker["raw_api_key"], job_id)

    # 2 non-matching partials, then a matching one (3 total at stop).
    _emit_partial(client, worker["raw_api_key"], job_id, {"severity": "low"})
    _emit_partial(client, worker["raw_api_key"], job_id, {"severity": "med"})
    _emit_partial(
        client, worker["raw_api_key"], job_id, {"severity": "critical"}
    )

    j = jobs.get_job(job_id)
    assert j["status"] == "stopped"
    assert j["partials_count"] == 3

    # With no max_units declared, partial settlement uses partials_count both
    # as numerator and denominator -> ratio is 1, so the agent earns the full
    # price and there is no caller refund. Default fee_bearer_policy charges
    # the caller the 10% platform fee on top of the 10c price (so they paid
    # 11c at submit). Agent earns 10c, platform fee 1c.
    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 189
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 10
    assert payments.get_wallet(platform_wallet["wallet_id"])["balance_cents"] == 1
