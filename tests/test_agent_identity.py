"""Tests for agent cryptographic identity (DIDs + signed outputs)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "https://aztea.ai")

from core import auth  # noqa: E402
from core import crypto  # noqa: E402
from core import disputes  # noqa: E402
from core import identity  # noqa: E402
from core import jobs  # noqa: E402
from core import payments  # noqa: E402
from core import registry  # noqa: E402
from core import reputation  # noqa: E402
import server.application as server  # noqa: E402


def _close_module_conn(module) -> None:
    conn = getattr(module._local, "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(module._local, "conn")
    except AttributeError:
        pass


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-identity-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)
    for m in modules:
        _close_module_conn(m)
        monkeypatch.setattr(m, "DB_PATH", str(db_path))
    monkeypatch.setenv("SERVER_BASE_URL", "https://aztea.ai")
    with TestClient(server.app):
        yield
    for m in modules:
        _close_module_conn(m)
    for suffix in ("", "-shm", "-wal"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            p.unlink()


def _new_user() -> dict:
    suffix = uuid.uuid4().hex[:8]
    return auth.register_user(
        username=f"id-{suffix}",
        email=f"id-{suffix}@example.com",
        password="password123",
    )


def _register_agent(owner_id: str, name: str | None = None) -> str:
    return registry.register_agent(
        name=name or f"id-agent-{uuid.uuid4().hex[:6]}",
        description="identity test agent",
        endpoint_url=f"https://example.com/{uuid.uuid4().hex[:6]}",
        price_per_call_usd=0.10,
        tags=["identity"],
        owner_id=owner_id,
        embed_listing=False,
    )


# ---------------------------------------------------------------------------
# Registration assigns DID + keypair
# ---------------------------------------------------------------------------

def test_register_agent_assigns_did_and_keypair():
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}", name="Identity Agent")
    agent = registry.get_agent(aid, include_unapproved=True)
    assert agent is not None
    assert agent["did"] == f"did:web:aztea.ai:agents:{aid}"
    assert agent["signing_public_key"].startswith("-----BEGIN PUBLIC KEY-----")
    assert agent["signing_private_key"].startswith("-----BEGIN PRIVATE KEY-----")
    assert agent["signing_alg"] == "ed25519"


def test_dids_are_unique_across_agents():
    user = _new_user()
    a1 = _register_agent(f"user:{user['user_id']}")
    a2 = _register_agent(f"user:{user['user_id']}")
    agent1 = registry.get_agent(a1, include_unapproved=True)
    agent2 = registry.get_agent(a2, include_unapproved=True)
    assert agent1["did"] != agent2["did"]
    assert agent1["signing_public_key"] != agent2["signing_public_key"]


def test_keypair_can_sign_and_verify_round_trip():
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    agent = registry.get_agent(aid, include_unapproved=True)
    payload = {"hello": "world", "n": 7}
    signature = crypto.sign_payload(agent["signing_private_key"], payload)
    assert crypto.verify_signature(agent["signing_public_key"], payload, signature) is True


# ---------------------------------------------------------------------------
# DID derivation (host extraction)
# ---------------------------------------------------------------------------

def test_build_agent_did_uses_server_base_url():
    assert (
        identity.build_agent_did("abc-123", server_base_url="https://example.com")
        == "did:web:example.com:agents:abc-123"
    )


def test_build_agent_did_encodes_localhost_port():
    # did:web requires the colon between host and port to be percent-encoded.
    did = identity.build_agent_did("abc", server_base_url="http://localhost:8000")
    assert did == "did:web:localhost%3A8000:agents:abc"


def test_build_agent_did_falls_back_to_default_host():
    did = identity.build_agent_did("abc", server_base_url=None)
    # Either env-derived or the default 'aztea.ai' — never blank.
    assert "did:web:" in did and ":agents:abc" in did


# ---------------------------------------------------------------------------
# update_job_status persists signature fields
# ---------------------------------------------------------------------------

def test_update_job_status_persists_signature_fields(monkeypatch):
    """Direct DB-layer test: writing a signature via update_job_status should
    show up on the job row."""
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")

    # Create a real job so we have a valid job_id to update.
    user2 = _new_user()
    caller_owner = f"user:{user2['user_id']}"
    caller_wallet = payments.get_or_create_wallet(caller_owner)
    payments.deposit(caller_wallet["wallet_id"], 1000, "test funds")
    agent_wallet = payments.get_or_create_wallet(f"agent:{aid}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    charge_tx = payments.pre_call_charge(caller_wallet["wallet_id"], 11, aid)
    job = jobs.create_job(
        agent_id=aid,
        agent_owner_id=f"user:{user['user_id']}",
        caller_owner_id=caller_owner,
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        price_cents=10,
        caller_charge_cents=11,
        platform_fee_pct_at_create=10,
        fee_bearer_policy="caller",
        charge_tx_id=charge_tx,
        input_payload={"task": "test"},
    )
    # Update with signature.
    updated = jobs.update_job_status(
        job["job_id"],
        "complete",
        output_payload={"answer": 42},
        completed=True,
        output_signature="A" * 88,
        output_signature_alg="ed25519",
        output_signed_by_did=f"did:web:aztea.ai:agents:{aid}",
        output_signed_at="2026-04-25T00:00:00+00:00",
    )
    assert updated is not None
    assert updated["output_signature"] == "A" * 88
    assert updated["output_signature_alg"] == "ed25519"
    assert updated["output_signed_by_did"] == f"did:web:aztea.ai:agents:{aid}"
    assert updated["output_signed_at"] == "2026-04-25T00:00:00+00:00"
