"""Regression test for Bug #8: sync calls must settle immediately (no verification block).

Balances must move on a successful sync builtin call — the old 72-hour dispute
window gate must NOT block settlement when AZTEA_REQUIRE_VERIFICATION is off (default).
"""

from tests.integration.support import *  # noqa: F403


def test_sync_builtin_call_settles_immediately(client, monkeypatch):
    """Agent wallet must grow immediately after a successful sync call.

    Pre-2026-05-17 this test used CVE Lookup as the priced builtin. Then
    CVE Lookup became a gateway free-tier agent (price=$0.00) and the
    balance-change assertion went mute — every sync call still settles
    correctly but with $0 amounts. Switched to Dependency Auditor so the
    "settlement is not blocked by the verification window" invariant
    still rides on a real money movement.
    """
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    # Stub the dependency auditor so the test doesn't fan out to OSV / PyPI.
    monkeypatch.setattr(
        server.agent_dependency_auditor,
        "run",
        lambda _payload, **_kwargs: {
            "ecosystem": "pypi",
            "findings": [],
            "total_packages": 1,
            "summary": "test::settlement_check",
        },
    )

    caller_owner = f"user:{caller['user_id']}"
    caller_wallet_id = payments.get_or_create_wallet(caller_owner)["wallet_id"]
    agent_wallet_id = payments.get_or_create_wallet(
        f"agent:{server._DEPENDENCY_AUDITOR_AGENT_ID}",
    )["wallet_id"]

    balance_before = payments.get_wallet(caller_wallet_id)["balance_cents"]
    agent_balance_before = payments.get_wallet(agent_wallet_id)["balance_cents"]

    resp = client.post(
        f"/registry/agents/{server._DEPENDENCY_AUDITOR_AGENT_ID}/call",
        json={"manifest": "requests==2.28.0\n", "ecosystem": "pypi"},
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("status") == "complete"
    assert "job_id" in body

    balance_after = payments.get_wallet(caller_wallet_id)["balance_cents"]
    agent_balance_after = payments.get_wallet(agent_wallet_id)["balance_cents"]

    assert balance_after < balance_before, (
        "Caller balance must decrease after a successful sync call"
    )
    assert agent_balance_after > agent_balance_before, (
        "Agent wallet must increase immediately — settlement must NOT be blocked "
        "by the 72-hour verification window when AZTEA_REQUIRE_VERIFICATION=0 (default)"
    )
