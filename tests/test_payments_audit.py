from __future__ import annotations

import sqlite3
import sys
import uuid

import pytest


def _close_conn(module) -> None:
    conn = getattr(getattr(module, "_local", None), "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(module._local, "conn")
    except AttributeError:
        pass


@pytest.fixture()
def payments_db(tmp_path, monkeypatch):
    from core import db as _db
    from core import payments

    db_path = str(tmp_path / f"payments-audit-{uuid.uuid4().hex}.db")

    for module in (_db, payments):
        _close_conn(module)
        monkeypatch.setattr(module, "DB_PATH", db_path)

    pkg = sys.modules.get("core.payments")
    if pkg is not None:
        monkeypatch.setattr(pkg, "DB_PATH", db_path, raising=False)

    with sqlite3.connect(db_path) as bootstrap:
        bootstrap.execute("PRAGMA journal_mode=WAL")

    payments.init_payments_db()
    yield payments

    for module in (_db, payments):
        _close_conn(module)


def _direct_charge(payments_mod, caller_wallet_id: str, amount_cents: int, agent_id: str) -> str:
    with sqlite3.connect(payments_mod._resolved_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        tx_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO transactions
                (tx_id, wallet_id, type, amount_cents, related_tx_id,
                 agent_id, charged_by_key_id, memo, created_at)
            VALUES (?, ?, 'charge', ?, NULL, ?, NULL, 'test charge', datetime('now'))
            """,
            (tx_id, caller_wallet_id, -int(amount_cents), agent_id),
        )
        conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents - ? WHERE wallet_id = ?",
            (int(amount_cents), caller_wallet_id),
        )
        conn.commit()
    return tx_id


def test_admin_transfer_uses_supported_types_and_stays_reconciled(payments_db):
    payments = payments_db
    source = payments.get_or_create_wallet("platform:test-source")
    dest = payments.get_or_create_wallet("platform:test-dest")
    payments.deposit(source["wallet_id"], 125, memo="seed")

    result = payments.admin_transfer(source["wallet_id"], dest["wallet_id"], 40, memo="sweep")
    assert result["amount_cents"] == 40

    source_latest = payments.get_wallet(source["wallet_id"])
    dest_latest = payments.get_wallet(dest["wallet_id"])
    assert source_latest is not None and source_latest["balance_cents"] == 85
    assert dest_latest is not None and dest_latest["balance_cents"] == 40

    txs = payments.get_wallet_transactions(source["wallet_id"], limit=10) + payments.get_wallet_transactions(dest["wallet_id"], limit=10)
    types = {tx["type"] for tx in txs}
    assert "charge" in types
    assert "deposit" in types
    assert "admin_withdraw" not in types
    assert "admin_deposit" not in types

    summary = payments.compute_ledger_invariants()
    assert summary["invariant_ok"] is True, summary


def test_wallet_balance_snapshot_and_repair_restore_cache(payments_db):
    payments = payments_db
    wallet = payments.get_or_create_wallet("user:drift")
    payments.deposit(wallet["wallet_id"], 100, memo="seed")

    with sqlite3.connect(payments._resolved_db_path()) as conn:
        conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents + 7 WHERE wallet_id = ?",
            (wallet["wallet_id"],),
        )

    snapshot = payments.get_wallet_balance_snapshot(wallet["wallet_id"])
    assert snapshot["cached_balance_cents"] == 107
    assert snapshot["ledger_balance_cents"] == 100
    assert snapshot["drift_cents"] == 7
    assert snapshot["invariant_ok"] is False

    summary = payments.compute_ledger_invariants()
    assert summary["invariant_ok"] is False
    assert summary["mismatch_count"] >= 1

    repaired = payments.repair_wallet_balance_cache(wallet["wallet_id"])
    assert repaired["cached_balance_cents"] == 100
    assert repaired["ledger_balance_cents"] == 100
    assert repaired["drift_cents"] == 0
    assert repaired["invariant_ok"] is True
    assert payments.compute_ledger_invariants()["invariant_ok"] is True


def test_variable_pricing_and_payout_curve_paths_stay_reconciled(payments_db):
    from core import payout_curve

    payments = payments_db
    caller_wallet = payments.get_or_create_wallet("user:caller")
    agent_wallet = payments.get_or_create_wallet("agent:test-agent")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    payments.deposit(caller_wallet["wallet_id"], 500, memo="seed")
    distribution = payments.compute_success_distribution(
        100,
        platform_fee_pct=10,
        fee_bearer_policy="caller",
    )
    caller_charge_cents = int(distribution["caller_charge_cents"])
    charge_tx_id = _direct_charge(payments, caller_wallet["wallet_id"], caller_charge_cents, "test-agent")
    payments.post_call_payout(
        agent_wallet["wallet_id"],
        platform_wallet["wallet_id"],
        charge_tx_id,
        100,
        "test-agent",
        platform_fee_pct=10,
        fee_bearer_policy="caller",
    )
    payments.post_call_refund_difference(
        caller_wallet["wallet_id"],
        charge_tx_id,
        55,
        "test-agent",
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        agent_clawback_cents=50,
        platform_clawback_cents=5,
        memo="half-usage",
    )
    clawback = payout_curve.apply_curve_clawback(
        job_id="job-payout-curve-3",
        agent_id="test-agent",
        agent_wallet_id=agent_wallet["wallet_id"],
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_payout_cents=50,
        payout_fraction=0.5,
    )
    assert clawback["applied"] is True

    summary = payments.compute_ledger_invariants()
    assert summary["invariant_ok"] is True, summary
    assert summary["wallet_total_cents"] == 500

