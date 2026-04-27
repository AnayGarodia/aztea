"""Regression test for Bug #8: sync calls must settle immediately (no verification block).

Balances must move on a successful sync builtin call — the old 72-hour dispute
window gate must NOT block settlement when AZTEA_REQUIRE_VERIFICATION is off (default).
"""

from tests.integration.support import *  # noqa: F403


def test_sync_builtin_call_settles_immediately(client, monkeypatch):
    """Agent wallet must grow immediately after a successful sync call."""
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    # Stub the CVE lookup agent so the test doesn't need live network access
    monkeypatch.setattr(
        server.agent_cve_lookup,
        "run",
        lambda _payload: {
            "results": [],
            "total_vulnerable": 0,
            "total_packages_checked": 1,
            "summary": "test::settlement_check",
            "source": "nvd",
            "billing_units_actual": 1,
        },
    )

    caller_owner = f"user:{caller['user_id']}"
    caller_wallet_id = payments.get_or_create_wallet(caller_owner)["wallet_id"]
    agent_wallet_id = payments.get_or_create_wallet(f"agent:{server._CVELOOKUP_AGENT_ID}")["wallet_id"]

    balance_before = payments.get_wallet(caller_wallet_id)["balance_cents"]
    agent_balance_before = payments.get_wallet(agent_wallet_id)["balance_cents"]

    resp = client.post(
        f"/registry/agents/{server._CVELOOKUP_AGENT_ID}/call",
        json={"packages": ["lodash@4.17.21"]},
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
