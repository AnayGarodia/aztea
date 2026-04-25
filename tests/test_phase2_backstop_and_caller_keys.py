"""Tests for Phase 2: agent caller keys and owner-backstop charge enforcement."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core import auth  # noqa: E402
from core import disputes  # noqa: E402
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
    db_path = Path(__file__).resolve().parent / f"test-phase2-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)
    for m in modules:
        _close_module_conn(m)
        monkeypatch.setattr(m, "DB_PATH", str(db_path))
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
        username=f"p2-{suffix}",
        email=f"p2-{suffix}@example.com",
        password="password123",
    )


def _register_agent(owner_id: str, *, name: str | None = None) -> str:
    return registry.register_agent(
        name=name or f"p2-agent-{uuid.uuid4().hex[:6]}",
        description="phase 2 test agent",
        endpoint_url=f"https://example.com/{uuid.uuid4().hex[:6]}",
        price_per_call_usd=0.10,
        tags=["p2"],
        owner_id=owner_id,
        embed_listing=False,
    )


# ---------------------------------------------------------------------------
# Agent caller keys
# ---------------------------------------------------------------------------

def test_create_agent_caller_key_returns_azac_prefix():
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    key = auth.create_agent_caller_api_key(aid, name="Test caller key")
    assert key["raw_key"].startswith("azac_")
    assert key["key_type"] == "caller"
    assert key["agent_id"] == aid


def test_verify_agent_caller_key_returns_caller_type():
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    key = auth.create_agent_caller_api_key(aid)
    verified = auth.verify_agent_api_key(key["raw_key"])
    assert verified is not None
    assert verified["key_type"] == "caller"
    assert verified["agent_id"] == aid


def test_verify_existing_worker_key_still_returns_worker_type():
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    worker_key = auth.create_agent_api_key(aid)
    verified = auth.verify_agent_api_key(worker_key["raw_key"])
    assert verified is not None
    assert verified["key_type"] == "worker"


def test_caller_key_rejected_when_agent_suspended():
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    key = auth.create_agent_caller_api_key(aid)
    registry.set_agent_status(aid, "suspended")
    assert auth.verify_agent_api_key(key["raw_key"]) is None


# ---------------------------------------------------------------------------
# Owner backstop in pre_call_charge
# ---------------------------------------------------------------------------

def test_charge_succeeds_from_balance_when_no_backstop_needed():
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")
    payments.deposit(agent_wallet["wallet_id"], 500, "earned")

    # No guarantor — straightforward charge.
    tx_id = payments.pre_call_charge(agent_wallet["wallet_id"], 200, aid)
    assert tx_id

    after = payments.get_wallet(agent_wallet["wallet_id"])
    assert int(after["balance_cents"]) == 300


def test_charge_fails_when_short_and_no_backstop():
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")
    payments.deposit(agent_wallet["wallet_id"], 50, "earned")
    with pytest.raises(payments.InsufficientBalanceError):
        payments.pre_call_charge(agent_wallet["wallet_id"], 200, aid)


def test_backstop_supplements_shortfall_from_parent():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    owner_wallet = payments.get_or_create_wallet(owner_id)
    payments.deposit(owner_wallet["wallet_id"], 1000, "owner balance")

    aid = _register_agent(owner_id)
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")
    payments.deposit(agent_wallet["wallet_id"], 50, "earned")
    payments.set_wallet_guarantor(agent_wallet["wallet_id"], enabled=True, cap_cents=500)

    # Charge 200; agent has 50, backstop covers 150.
    tx_id = payments.pre_call_charge(agent_wallet["wallet_id"], 200, aid)
    assert tx_id

    after_agent = payments.get_wallet(agent_wallet["wallet_id"])
    after_owner = payments.get_wallet(owner_wallet["wallet_id"])
    assert int(after_agent["balance_cents"]) == 0
    # Owner started at 1000, supplied 150 → 850.
    assert int(after_owner["balance_cents"]) == 850


def test_backstop_blocked_when_daily_cap_exhausted():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    owner_wallet = payments.get_or_create_wallet(owner_id)
    payments.deposit(owner_wallet["wallet_id"], 10_000, "owner balance")

    aid = _register_agent(owner_id)
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")
    payments.set_wallet_guarantor(agent_wallet["wallet_id"], enabled=True, cap_cents=100)

    # First call: agent has 0, cap 100 — backstop 100, charge 100. OK.
    payments.pre_call_charge(agent_wallet["wallet_id"], 100, aid)

    # Second call: cap exhausted, balance still 0 → must fail.
    with pytest.raises(payments.InsufficientBalanceError):
        payments.pre_call_charge(agent_wallet["wallet_id"], 1, aid)


def test_backstop_disabled_does_nothing_even_with_parent():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    owner_wallet = payments.get_or_create_wallet(owner_id)
    payments.deposit(owner_wallet["wallet_id"], 1000, "owner balance")

    aid = _register_agent(owner_id)
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")
    # Guarantor stays disabled (default).
    with pytest.raises(payments.InsufficientBalanceError):
        payments.pre_call_charge(agent_wallet["wallet_id"], 200, aid)


def test_backstop_uncapped_cap_uses_only_parent_balance_as_limit():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    owner_wallet = payments.get_or_create_wallet(owner_id)
    payments.deposit(owner_wallet["wallet_id"], 300, "owner balance")

    aid = _register_agent(owner_id)
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")
    payments.set_wallet_guarantor(agent_wallet["wallet_id"], enabled=True, cap_cents=None)

    # Agent balance 0; need 250 → backstop pulls 250 from parent (which has 300).
    payments.pre_call_charge(agent_wallet["wallet_id"], 250, aid)
    assert int(payments.get_wallet(owner_wallet["wallet_id"])["balance_cents"]) == 50
    assert int(payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"]) == 0
