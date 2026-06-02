"""Tests for the web_actions fail-forward state machine (E2 foundation).

The crux: a worker can crash mid-action, so the durable commit_phase must let a
sweeper recover correctly — refund only what never submitted, settle (never refund)
anything that did. These tests pin that invariant at the transition level.
"""

from __future__ import annotations

import pytest

from core import web_actions as wa


@pytest.fixture()
def wadb(tmp_path, monkeypatch):
    monkeypatch.setattr(wa, "DB_PATH", str(tmp_path / "web_actions.db"))
    wa.init_web_actions_db()
    return wa


def test_reconcile_action_is_fail_forward():
    assert wa.reconcile_action("pre_submit") == "refund"
    assert wa.reconcile_action("submitted") == "settle_forward"  # caller already paid -> settle
    assert wa.reconcile_action("settled") == "skip"
    assert wa.reconcile_action("anything-else") == "skip"


def test_create_starts_pre_submit_executing(wadb):
    row = wadb.create_web_action(
        mandate_id="amd_x", target_domain="shop.example.com",
        quoted_cost_cents=500, agent_fee_cents=50,
    )
    assert row["phase"] == "executing" and row["commit_phase"] == "pre_submit"
    assert row["mandate_id"] == "amd_x" and row["quoted_cost_cents"] == 500


def test_mark_submitted_is_once_only(wadb):
    a = wadb.create_web_action(mandate_id="amd_x")
    assert wadb.mark_submitted(a["action_id"]) is True
    assert wadb.get_web_action(a["action_id"])["commit_phase"] == "submitted"
    assert wadb.mark_submitted(a["action_id"]) is False  # already submitted -> idempotent


def test_fail_forward_cannot_refund_a_submitted_action(wadb):
    a = wadb.create_web_action(mandate_id="amd_x")
    wadb.mark_submitted(a["action_id"])
    # The merchant action happened -> a refund is REFUSED (fail-forward); only settle.
    assert wadb.settle_refunded(a["action_id"], failure_code="x") is False
    assert wadb.settle_completed(a["action_id"], actual_cost_cents=400, platform_fee_cents=5) is True
    row = wadb.get_web_action(a["action_id"])
    assert row["commit_phase"] == "settled" and row["phase"] == "completed"


def test_cannot_settle_completed_a_pre_submit_action(wadb):
    a = wadb.create_web_action(mandate_id="amd_x")
    # Nothing submitted yet -> can't mark it completed/paid; only refund is allowed.
    assert wadb.settle_completed(a["action_id"], actual_cost_cents=1, platform_fee_cents=0) is False
    assert wadb.settle_refunded(a["action_id"], failure_code="user_abort") is True
    row = wadb.get_web_action(a["action_id"])
    assert row["commit_phase"] == "settled" and row["phase"] == "failed"


def test_list_stale_unsettled_finds_executing_then_drops_settled(wadb):
    a = wadb.create_web_action(mandate_id="amd_x")
    far_future = "2999-01-01T00:00:00+00:00"
    assert any(r["action_id"] == a["action_id"] for r in wadb.list_stale_unsettled(far_future))
    wadb.settle_refunded(a["action_id"], failure_code="x")  # now settled
    assert not any(r["action_id"] == a["action_id"] for r in wadb.list_stale_unsettled(far_future))
