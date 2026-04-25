"""Tests for per-agent sub-wallets (Phase 1).

Covers:
- ``register_agent`` eagerly creates a child wallet linked to the owner.
- ``list_child_wallets`` returns all sub-wallets under a parent.
- ``set_wallet_guarantor`` and ``set_wallet_label`` mutate metadata correctly.
- ``sweep_to_parent`` moves balance atomically and preserves the ledger invariant.
- ``get_agent_earnings_breakdown_v2`` aggregates payouts across child wallets.
"""

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
    """Run each test against a fresh SQLite DB so wallets / agents don't leak."""
    db_path = Path(__file__).resolve().parent / f"test-subwallets-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)
    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))

    # Triggers schema init for every module via the FastAPI startup hooks.
    with TestClient(server.app):
        yield

    for module in modules:
        _close_module_conn(module)
    for suffix in ("", "-shm", "-wal"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            p.unlink()


def _new_user() -> dict:
    suffix = uuid.uuid4().hex[:8]
    return auth.register_user(
        username=f"swt-{suffix}",
        email=f"swt-{suffix}@example.com",
        password="password123",
    )


def _register_agent(owner_id: str, *, name: str | None = None) -> str:
    return registry.register_agent(
        name=name or f"swt-agent-{uuid.uuid4().hex[:6]}",
        description="sub-wallet test agent",
        endpoint_url=f"https://example.com/{uuid.uuid4().hex[:6]}",
        price_per_call_usd=0.10,
        tags=["sub-wallet-test"],
        owner_id=owner_id,
        embed_listing=False,
    )


def test_register_agent_creates_linked_subwallet():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    owner_wallet = payments.get_or_create_wallet(owner_id)
    aid = _register_agent(owner_id, name="Linked agent")

    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")
    assert agent_wallet is not None, "agent sub-wallet should exist after registration"
    assert agent_wallet["parent_wallet_id"] == owner_wallet["wallet_id"]
    assert agent_wallet["display_label"] == "Linked agent"
    assert int(agent_wallet["balance_cents"]) == 0


def test_list_child_wallets_returns_all_owned_agents():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    owner_wallet = payments.get_or_create_wallet(owner_id)
    a1 = _register_agent(owner_id, name="agent A")
    a2 = _register_agent(owner_id, name="agent B")
    a3 = _register_agent(owner_id, name="agent C")

    children = payments.list_child_wallets(owner_wallet["wallet_id"])
    owners = {row["owner_id"] for row in children}
    assert f"agent:{a1}" in owners
    assert f"agent:{a2}" in owners
    assert f"agent:{a3}" in owners


def test_set_wallet_guarantor_persists_and_validates():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    payments.get_or_create_wallet(owner_id)
    aid = _register_agent(owner_id)
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")

    updated = payments.set_wallet_guarantor(
        agent_wallet["wallet_id"], enabled=True, cap_cents=500
    )
    assert int(updated["guarantor_enabled"]) == 1
    assert int(updated["guarantor_cap_cents"]) == 500

    cleared = payments.set_wallet_guarantor(
        agent_wallet["wallet_id"], enabled=False, cap_cents=None
    )
    assert int(cleared["guarantor_enabled"]) == 0
    assert cleared["guarantor_cap_cents"] is None

    with pytest.raises(ValueError):
        payments.set_wallet_guarantor(
            agent_wallet["wallet_id"], enabled=True, cap_cents=-1
        )


def test_set_wallet_label_trims_and_clears():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    payments.get_or_create_wallet(owner_id)
    aid = _register_agent(owner_id, name="x")
    wallet_id = payments.get_wallet_by_owner(f"agent:{aid}")["wallet_id"]

    after = payments.set_wallet_label(wallet_id, "  My production bot  ")
    assert after["display_label"] == "My production bot"

    cleared = payments.set_wallet_label(wallet_id, "")
    assert cleared["display_label"] is None


def test_sweep_to_parent_full_balance_moves_funds_atomically():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    owner_wallet = payments.get_or_create_wallet(owner_id)
    aid = _register_agent(owner_id)
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")

    # Simulate the agent earning 1000 cents.
    payments.deposit(agent_wallet["wallet_id"], 1000, "earned")

    starting_owner_balance = int(payments.get_wallet(owner_wallet["wallet_id"])["balance_cents"])
    result = payments.sweep_to_parent(agent_wallet["wallet_id"])
    assert result["amount_cents"] == 1000

    after_agent = payments.get_wallet(agent_wallet["wallet_id"])
    after_owner = payments.get_wallet(owner_wallet["wallet_id"])
    assert int(after_agent["balance_cents"]) == 0
    assert int(after_owner["balance_cents"]) == starting_owner_balance + 1000


def test_sweep_to_parent_partial_amount():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    payments.get_or_create_wallet(owner_id)
    aid = _register_agent(owner_id)
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")

    payments.deposit(agent_wallet["wallet_id"], 1000, "earned")
    result = payments.sweep_to_parent(agent_wallet["wallet_id"], amount_cents=600)
    assert result["amount_cents"] == 600

    refreshed = payments.get_wallet(agent_wallet["wallet_id"])
    assert int(refreshed["balance_cents"]) == 400


def test_sweep_zero_balance_is_noop():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    payments.get_or_create_wallet(owner_id)
    aid = _register_agent(owner_id)
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")

    result = payments.sweep_to_parent(agent_wallet["wallet_id"])
    assert result["amount_cents"] == 0
    assert result["sweep_tx_id"] is None


def test_sweep_orphan_wallet_raises():
    # Create a wallet with no parent_wallet_id (e.g. a built-in agent wallet).
    orphan = payments.get_or_create_wallet(f"agent:{uuid.uuid4().hex}")
    payments.deposit(orphan["wallet_id"], 100, "earned by orphan")
    with pytest.raises(ValueError):
        payments.sweep_to_parent(orphan["wallet_id"])


def test_sweep_amount_exceeds_balance_raises():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    payments.get_or_create_wallet(owner_id)
    aid = _register_agent(owner_id)
    agent_wallet = payments.get_wallet_by_owner(f"agent:{aid}")

    payments.deposit(agent_wallet["wallet_id"], 100, "earned")
    with pytest.raises(payments.InsufficientBalanceError):
        payments.sweep_to_parent(agent_wallet["wallet_id"], amount_cents=200)


def test_get_agent_earnings_breakdown_v2_aggregates_subwallets():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    owner_wallet = payments.get_or_create_wallet(owner_id)
    a1 = _register_agent(owner_id, name="earner one")
    a2 = _register_agent(owner_id, name="earner two")

    w1 = payments.get_wallet_by_owner(f"agent:{a1}")["wallet_id"]
    w2 = payments.get_wallet_by_owner(f"agent:{a2}")["wallet_id"]

    # Use _insert_tx via a 'payout' transaction so the breakdown's payout-aggregator picks it up.
    with payments._conn() as conn:
        payments._insert_tx(conn, w1, "payout", 500, a1, None, "test payout 1")
        payments._insert_tx(conn, w1, "payout", 200, a1, None, "test payout 1b")
        payments._insert_tx(conn, w2, "payout", 100, a2, None, "test payout 2")

    breakdown = payments.get_agent_earnings_breakdown_v2(owner_wallet["wallet_id"])
    by_id = {row["agent_id"]: row for row in breakdown}
    assert by_id[a1]["total_earned_cents"] == 700
    assert by_id[a1]["call_count"] == 2
    assert by_id[a2]["total_earned_cents"] == 100
    assert by_id[a2]["call_count"] == 1
    # Balances should match what we just credited.
    assert by_id[a1]["current_balance_cents"] == 700
    assert by_id[a2]["current_balance_cents"] == 100


def test_get_agent_earnings_breakdown_v2_includes_zero_earners():
    user = _new_user()
    owner_id = f"user:{user['user_id']}"
    owner_wallet = payments.get_or_create_wallet(owner_id)
    aid = _register_agent(owner_id, name="brand new agent")

    breakdown = payments.get_agent_earnings_breakdown_v2(owner_wallet["wallet_id"])
    assert any(row["agent_id"] == aid for row in breakdown), (
        "newly registered agent should appear with zero earnings"
    )
    row = next(r for r in breakdown if r["agent_id"] == aid)
    assert row["total_earned_cents"] == 0
    assert row["call_count"] == 0
    assert row["current_balance_cents"] == 0
