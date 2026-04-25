"""Integration tests for agent sub-wallet HTTP endpoints."""

from tests.integration.support import *  # noqa: F403


def test_wallets_me_agents_lists_owned_subwallets(client):
    owner = _register_user()
    aid_1 = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"SubWallet Agent A {uuid.uuid4().hex[:6]}",
        price=0.10,
    )
    aid_2 = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"SubWallet Agent B {uuid.uuid4().hex[:6]}",
        price=0.05,
    )

    resp = client.get(
        "/wallets/me/agents",
        headers=_auth_headers(owner["raw_api_key"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_id = {row["agent_id"]: row for row in body["agents"]}
    assert aid_1 in by_id and aid_2 in by_id
    for aid in (aid_1, aid_2):
        row = by_id[aid]
        assert row["current_balance_cents"] == 0
        assert row["call_count"] == 0
        assert row["wallet_id"]
        assert row["guarantor_enabled"] is False


def test_patch_agent_wallet_settings_updates_metadata(client):
    owner = _register_user()
    aid = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"SubWallet Settings {uuid.uuid4().hex[:6]}",
    )

    resp = client.patch(
        f"/wallets/agents/{aid}/settings",
        headers=_auth_headers(owner["raw_api_key"]),
        json={
            "display_label": "Production reviewer",
            "guarantor_enabled": True,
            "guarantor_cap_cents": 750,
            "daily_spend_limit_cents": 1500,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_label"] == "Production reviewer"
    assert body["guarantor_enabled"] is True
    assert body["guarantor_cap_cents"] == 750
    assert body["daily_spend_limit_cents"] == 1500


def test_patch_agent_wallet_rejects_non_owner(client):
    owner = _register_user()
    intruder = _register_user()
    aid = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"SubWallet Owner Check {uuid.uuid4().hex[:6]}",
    )
    resp = client.patch(
        f"/wallets/agents/{aid}/settings",
        headers=_auth_headers(intruder["raw_api_key"]),
        json={"display_label": "hijacked"},
    )
    assert resp.status_code == 404, resp.text


def test_sweep_agent_wallet_to_owner(client):
    owner = _register_user()
    aid = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"SubWallet Sweep {uuid.uuid4().hex[:6]}",
    )

    # Credit the agent's sub-wallet directly so we can sweep something.
    agent_wallet = payments.get_or_create_wallet(f"agent:{aid}")
    payments.deposit(agent_wallet["wallet_id"], 800, "test earnings")

    owner_wallet = payments.get_or_create_wallet(f"user:{owner['user_id']}")
    starting_owner_balance = int(
        payments.get_wallet(owner_wallet["wallet_id"])["balance_cents"]
    )

    resp = client.post(
        f"/wallets/agents/{aid}/sweep",
        headers=_auth_headers(owner["raw_api_key"]),
        json={},  # omit amount → sweep full balance
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["amount_cents"] == 800

    after_agent = payments.get_wallet(agent_wallet["wallet_id"])
    after_owner = payments.get_wallet(owner_wallet["wallet_id"])
    assert int(after_agent["balance_cents"]) == 0
    assert int(after_owner["balance_cents"]) == starting_owner_balance + 800


def test_get_agent_wallet_transactions_returns_recent(client):
    owner = _register_user()
    aid = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"SubWallet Tx {uuid.uuid4().hex[:6]}",
    )
    agent_wallet = payments.get_or_create_wallet(f"agent:{aid}")
    payments.deposit(agent_wallet["wallet_id"], 100, "tx 1")
    payments.deposit(agent_wallet["wallet_id"], 250, "tx 2")

    resp = client.get(
        f"/wallets/agents/{aid}/transactions?limit=10",
        headers=_auth_headers(owner["raw_api_key"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == aid
    assert len(body["transactions"]) >= 2
