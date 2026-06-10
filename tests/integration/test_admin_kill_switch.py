"""Kill switch — `/admin/agents/{id}/suspend` must immediately refund
in-flight calls.

Wave 3 platform pivot makes the suspend endpoint the operator's
incident-response button for hosted code execution. Before this
change, suspend only flipped the agent status and left any in-flight
job in 'pending' / 'running' state, racing the next claim. Callers
charged for those jobs would never see output and never get refunded.
After this change suspend behaves like ban for in-flight jobs: fail
them, refund the caller's escrow, emit a structured audit log line.
"""

from __future__ import annotations

import uuid

from tests.integration.support import *  # noqa: F403
from tests.integration.helpers import _register_agent_via_api


def _register_agent(client, owner_key: str, name: str) -> str:
    return _register_agent_via_api(client, owner_key, name=name, price=0.05)


def test_kill_switch_refunds_in_flight_jobs(client):
    """An async job that was already created (charge held in escrow)
    must be failed + refunded when the admin trips the kill switch."""
    owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)  # 300 cents = $3.00
    # Title-case "Ks" prefix, not "KS": the agent-name validator rejects names whose
    # letters are >=80% uppercase, and a uuid hex suffix is sometimes all-digits, which
    # would leave "KS" as the only letters (100% caps) and flake ~6% of runs.
    agent_id = _register_agent(client, owner["raw_api_key"], f"Ks {uuid.uuid4().hex[:6]}")

    # Submit a job — this pre-charges the caller wallet for the agent price.
    created = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "x"}},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]
    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    pre_kill = payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"]
    assert pre_kill < 300, "Pre-kill balance should reflect the pre-call charge"

    # Hit the kill switch.
    suspended = client.post(
        f"/admin/agents/{agent_id}/suspend",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"reason": "investigation in progress"},
    )
    assert suspended.status_code == 200, suspended.text
    body = suspended.json()
    assert body["agent"]["status"] == "suspended"
    summary = body["kill_switch_summary"]
    assert summary["affected_jobs"] >= 1
    assert summary["refunded_jobs"] >= 1

    # The job is now failed.
    job_state = client.get(
        f"/jobs/{job_id}",
        headers=_auth_headers(caller["raw_api_key"]),
    ).json()
    assert job_state["status"] == "failed"

    # And the caller's wallet got the refund back.
    post_kill = payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"]
    assert post_kill == 300, f"Expected full refund (300), got {post_kill}"


def test_kill_switch_blocks_subsequent_calls(client):
    """After kill-switch, new job submissions must be refused at the
    create path with `agent.suspended` — this was already true for the
    old suspend behavior; verify the new envelope didn't break it."""
    owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    agent_id = _register_agent(client, owner["raw_api_key"], f"Ks2 {uuid.uuid4().hex[:6]}")

    suspended = client.post(
        f"/admin/agents/{agent_id}/suspend",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert suspended.status_code == 200, suspended.text

    blocked = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "should fail"}},
    )
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()["error"] == "agent.suspended"


def test_kill_switch_requires_admin_scope(client):
    """A non-master, non-admin key must be refused 403. Defense-in-depth
    around the highest-blast-radius admin endpoint."""
    owner = _register_user()
    agent_id = _register_agent(client, owner["raw_api_key"], f"Ks3 {uuid.uuid4().hex[:6]}")
    # Use the owner's own (non-admin) key.
    resp = client.post(
        f"/admin/agents/{agent_id}/suspend",
        headers=_auth_headers(owner["raw_api_key"]),
    )
    assert resp.status_code == 403, resp.text


def test_kill_switch_unknown_agent_returns_structured_404(client):
    """The new endpoint uses the structured error envelope. Verifies the
    detail body is the make_error shape, not a bare string."""
    bogus = "00000000-0000-0000-0000-000000000000"
    resp = client.post(
        f"/admin/agents/{bogus}/suspend",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    # The platform's exception handler unwraps make_error envelopes to
    # the top level (see core/error_handlers / part_001).
    assert body.get("error") == "agent.not_found"
    assert body.get("details", {}).get("agent_id") == bogus
