from __future__ import annotations

from core import jobs
from core import payments
from core import registry
from tests.integration.helpers import (
    _auth_headers,
    _fund_user_wallet,
    _register_agent_via_api,
    _register_user,
)


def test_registry_search_and_manifests_expose_privacy_and_client_routing(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, amount_cents=2_000)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name="Privacy First Agent",
        tags=["privacy", "routing"],
    )

    update = client.patch(
        f"/registry/agents/{agent_id}",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "pii_safe": True,
            "outputs_not_stored": True,
            "audit_logged": True,
            "region_locked": "us",
        },
    )
    assert update.status_code == 200, update.text
    updated_agent = update.json()
    assert updated_agent["pii_safe"] is True
    assert updated_agent["region_locked"] == "us"

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    job_a = jobs.create_job(
        agent_id=agent_id,
        caller_owner_id=f"user:{caller['user_id']}",
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        price_cents=10,
        caller_charge_cents=11,
        charge_tx_id="tx-claude",
        input_payload={"task": "route"},
        agent_owner_id=f"user:{worker['user_id']}",
        client_id="claude-code",
        max_attempts=1,
    )
    jobs.update_job_status(job_a["job_id"], "complete", output_payload={"ok": True}, completed=True)

    job_b = jobs.create_job(
        agent_id=agent_id,
        caller_owner_id=f"user:{caller['user_id']}",
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        price_cents=10,
        caller_charge_cents=11,
        charge_tx_id="tx-codex",
        input_payload={"task": "route"},
        agent_owner_id=f"user:{worker['user_id']}",
        client_id="codex",
        max_attempts=1,
    )
    jobs.update_job_status(job_b["job_id"], "failed", error_message="boom", completed=True)

    agent = registry.get_agent_with_reputation(agent_id, include_unapproved=True)
    assert agent is not None
    assert agent["by_client"]["claude-code"] > agent["by_client"]["codex"]

    search = client.post(
        "/registry/search",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "query": "privacy compliant agent",
            "pii_safe": True,
            "outputs_not_stored": True,
            "audit_logged": True,
            "region_locked": "us",
        },
    )
    assert search.status_code == 200, search.text
    results = search.json()["results"]
    assert any(item["agent"]["agent_id"] == agent_id for item in results)

    detail = client.get(
        f"/registry/agents/{agent_id}",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["pii_safe"] is True
    assert payload["outputs_not_stored"] is True
    assert payload["audit_logged"] is True
    assert payload["by_client"]["claude-code"] > payload["by_client"]["codex"]

    codex = client.get("/codex/tools", headers=_auth_headers(caller["raw_api_key"]))
    assert codex.status_code == 200, codex.text
    tool_lookup = codex.json()["tool_lookup"]
    registry_tool_name = next(
        name for name, meta in tool_lookup.items() if meta.get("agent_id") == agent_id
    )
    assert tool_lookup[registry_tool_name]["trust_score_by_client"]["claude-code"] > 0

    mcp_manifest = client.get("/mcp/manifest", headers=_auth_headers(caller["raw_api_key"]))
    assert mcp_manifest.status_code == 200, mcp_manifest.text
    review_tool = next(
        tool for tool in mcp_manifest.json()["tools"] if tool["name"] == registry_tool_name
    )
    assert "Privacy:" in review_tool["description"]
    assert "client trust:" in review_tool["description"]
