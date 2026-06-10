"""Pure settlement math for escrowed web actions (Phase 4). Integer cents only.

*** NOT WIRED in this PR: pure math only — no ledger caller moves money through this yet.
The live reserve/settle/refund wiring is the deferred write-web money-PR. ***

# OWNS: the cap check, and the caller-charge / agent-payout / platform-fee split
#        for a completed action.
# NOT OWNS: the ledger writes (the live settlement path reuses core.payments
#           primitives under BEGIN IMMEDIATE with the standard double-settlement
#           guards — that wiring is a focused money-PR), and the mandate lifecycle.
# INVARIANTS:
#   * No float(), ever — this module is money-adjacent.
#   * Platform fee is taken on the AGENT FEE only; the merchant cost is a
#     pass-through, so the platform never skims the ticket.
#   * agent_payout + platform_fee == caller_charge (the books always balance).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionSettlement:
    """The integer-cent split of a completed action."""
    caller_charge_cents: int   # actual merchant cost + agent service fee
    agent_payout_cents: int    # cost pass-through + fee, net of the platform's cut
    platform_fee_cents: int    # platform's cut, taken on the agent fee only


def within_cap(*, actual_cost_cents: int, agent_fee_cents: int, max_spend_cents: int) -> bool:
    """Pure: does actual cost + agent fee fit under the mandate cap? Integer cents."""
    return (int(actual_cost_cents) + int(agent_fee_cents)) <= int(max_spend_cents)


def compute_action_settlement(
    *, actual_cost_cents: int, agent_fee_cents: int, platform_fee_pct: int,
) -> ActionSettlement:
    """Pure: split a completed action into caller charge / agent payout / platform fee.

    Platform fee = floor(agent_fee * pct / 100), taken out of the agent fee. The
    merchant cost passes straight through to the agent (who fronted it). Raises on
    negative or out-of-range inputs (fail loud at the boundary).
    """
    cost = int(actual_cost_cents)
    fee = int(agent_fee_cents)
    pct = int(platform_fee_pct)
    if cost < 0 or fee < 0 or not (0 <= pct <= 100):
        raise ValueError("invalid settlement inputs (negative cents or pct out of 0..100)")
    platform_fee = (fee * pct) // 100  # integer cents, floor — never float
    caller_charge = cost + fee
    agent_payout = cost + fee - platform_fee
    return ActionSettlement(
        caller_charge_cents=caller_charge,
        agent_payout_cents=agent_payout,
        platform_fee_cents=platform_fee,
    )
