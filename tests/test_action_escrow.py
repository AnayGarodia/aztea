"""Pure settlement-math tests for the escrowed write-web (Phase 4).

Integer cents only; platform fee on the agent fee only (merchant cost passes
through); the books always balance; bad inputs fail loud.
"""

from __future__ import annotations

import pytest

from core.payments import action_escrow as ae


def test_within_cap():
    assert ae.within_cap(actual_cost_cents=900, agent_fee_cents=100, max_spend_cents=1000) is True
    assert ae.within_cap(actual_cost_cents=950, agent_fee_cents=100, max_spend_cents=1000) is False


def test_settlement_fee_on_fee_only_and_balances():
    s = ae.compute_action_settlement(actual_cost_cents=2000, agent_fee_cents=100, platform_fee_pct=10)
    assert s.platform_fee_cents == 10          # 10% of the 100c fee, NOT of 2100c
    assert s.caller_charge_cents == 2100       # merchant cost + fee
    assert s.agent_payout_cents == 2090        # cost pass-through + fee - platform cut
    assert s.agent_payout_cents + s.platform_fee_cents == s.caller_charge_cents


def test_settlement_integer_floor():
    s = ae.compute_action_settlement(actual_cost_cents=0, agent_fee_cents=15, platform_fee_pct=10)
    assert s.platform_fee_cents == 1           # floor(15*10/100) — integer, never float


def test_settlement_rejects_bad_inputs():
    with pytest.raises(ValueError):
        ae.compute_action_settlement(actual_cost_cents=-1, agent_fee_cents=0, platform_fee_pct=10)
    with pytest.raises(ValueError):
        ae.compute_action_settlement(actual_cost_cents=0, agent_fee_cents=0, platform_fee_pct=200)
