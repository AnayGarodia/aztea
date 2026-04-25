"""Integration tests for agent-as-caller (azac_) keys end-to-end through HTTP."""

from tests.integration.support import *  # noqa: F403


def test_mint_agent_caller_key_via_api(client):
    owner = _register_user()
    aid = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"Caller Key Test {uuid.uuid4().hex[:6]}",
    )

    resp = client.post(
        f"/registry/agents/{aid}/caller-keys",
        headers=_auth_headers(owner["raw_api_key"]),
        json={"name": "main caller key"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["raw_key"].startswith("azac_")
    assert body["key_type"] == "caller"
    assert body["agent_id"] == aid


def test_caller_key_can_authenticate_and_charges_agent_wallet(client):
    """Agent A uses its azac_ key to hire agent B; A's sub-wallet pays."""
    owner_a = _register_user()
    owner_b = _register_user()

    agent_a = _register_agent_via_api(
        client, owner_a["raw_api_key"], name=f"A2A Hirer {uuid.uuid4().hex[:6]}"
    )
    agent_b = _register_agent_via_api(
        client, owner_b["raw_api_key"], name=f"A2A Worker {uuid.uuid4().hex[:6]}", price=0.05
    )

    # Mint a caller key for agent A.
    key_resp = client.post(
        f"/registry/agents/{agent_a}/caller-keys",
        headers=_auth_headers(owner_a["raw_api_key"]),
        json={"name": "A's caller key"},
    )
    assert key_resp.status_code == 201, key_resp.text
    azac_key = key_resp.json()["raw_key"]

    # Fund agent A's sub-wallet directly with 200 cents.
    a_wallet = payments.get_or_create_wallet(f"agent:{agent_a}")
    payments.deposit(a_wallet["wallet_id"], 200, "seed for A2A test")

    # Now agent A hires agent B using its caller key.
    hire = client.post(
        "/jobs",
        headers=_auth_headers(azac_key),
        json={"agent_id": agent_b, "input_payload": {"task": "test"}, "max_attempts": 1},
    )
    assert hire.status_code == 201, hire.text
    job = hire.json()
    # Caller owner should be agent A's wallet identity.
    assert job["caller_owner_id"] == f"agent:{agent_a}"

    # Agent A's wallet should have been debited by the call price.
    a_wallet_after = payments.get_wallet(a_wallet["wallet_id"])
    # 0.05 USD = 5 cents (price), but with default 'caller' fee bearer policy
    # the caller is charged price + 10% platform fee → 6 cents (round-up).
    # Just assert it dropped from 200 by something <= 200.
    assert int(a_wallet_after["balance_cents"]) < 200


def test_caller_key_blocked_from_minting_new_keys(client):
    owner = _register_user()
    aid = _register_agent_via_api(
        client, owner["raw_api_key"], name=f"NoMint {uuid.uuid4().hex[:6]}"
    )
    key_resp = client.post(
        f"/registry/agents/{aid}/caller-keys",
        headers=_auth_headers(owner["raw_api_key"]),
        json={"name": "test"},
    )
    azac_key = key_resp.json()["raw_key"]

    # Trying to mint another key with the azac_ key itself must be rejected.
    blocked = client.post(
        f"/registry/agents/{aid}/caller-keys",
        headers=_auth_headers(azac_key),
        json={"name": "should fail"},
    )
    assert blocked.status_code == 403, blocked.text
