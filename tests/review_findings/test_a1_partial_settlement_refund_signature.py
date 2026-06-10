"""Finding A1 (2026-05-30 review): `_settle_partial_units` calls `post_call_refund`
with a non-existent signature, so the caller's remainder refund crashes with a
TypeError on the partial-settlement path.

`core/payments/base.py:1361` defines:
    post_call_refund(caller_wallet_id, charge_tx_id, price_cents, agent_id) -> None

`core/settlement_runner.py:327` calls it with `refund_cents=` and `reason=` (neither
is a parameter) and omits the required `price_cents=`. The agent payout at :317 runs
first, so when this fires the agent is paid for partial work but the caller's remainder
is never refunded — the function dies after the payout.

This is a DEMONSTRATION test (report-only). It is expected to FAIL against the current
source until the call site is fixed. Once fixed, flip the body to assert the refund is
issued with the correct amount.
"""

from __future__ import annotations

import inspect

import pytest


def test_post_call_refund_signature_has_no_refund_cents_or_reason():
    """Static proof: the kwargs used at settlement_runner.py:327 are not real params."""
    from core.payments.base import post_call_refund

    params = set(inspect.signature(post_call_refund).parameters)
    # The call site uses these — they must NOT exist for the bug to be real.
    assert "refund_cents" not in params, (
        "If post_call_refund grew a refund_cents param, A1 may be fixed — revisit."
    )
    assert "reason" not in params
    # And price_cents IS required but is omitted at the call site.
    assert "price_cents" in params


def test_settle_partial_units_raises_typeerror_on_refund(monkeypatch):
    """Behavioral proof: a partial settlement with a nonzero remainder raises TypeError.

    We stub `post_call_payout` (it runs first and would hit the DB) and leave the real
    `post_call_refund` in place — Python rejects the bogus kwargs at bind time, before
    any DB work, so this test needs no database.
    """
    import core.payments.base as base
    import core.settlement_runner as sr

    payout_calls = []
    monkeypatch.setattr(
        base, "post_call_payout", lambda **kw: payout_calls.append(kw)
    )

    # price=100, partials=1, declared_max=2  ->  settle=50, refund=50  (refund_cents > 0)
    job = {
        "price_cents": 100,
        "partials_count": 1,
        "stop_when_json": '{"max_units": 2}',
        "caller_wallet_id": "w_caller",
        "agent_wallet_id": "w_agent",
        "platform_wallet_id": "w_platform",
        "charge_tx_id": "tx_charge_123",
        "agent_id": "agent_x",
        "platform_fee_pct_at_create": 10,
        "fee_bearer_policy": None,
    }

    with pytest.raises(TypeError) as excinfo:
        sr._settle_partial_units(job)

    # The agent payout ran (proving the refund crash leaves the agent paid)...
    assert payout_calls, "expected post_call_payout to run before the refund crash"
    # ...and the crash is specifically the bad refund kwargs.
    assert "refund_cents" in str(excinfo.value) or "price_cents" in str(excinfo.value)
