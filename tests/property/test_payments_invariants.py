"""Hypothesis property tests for the pure money functions in core/payments.

# OWNS: invariants on compute_platform_fee_cents, compute_success_distribution,
#       normalize_fee_bearer_policy, parse_curve, fraction_for_rating,
#       curve_to_json. All targets are pure (no DB).
# INVARIANTS asserted: integer-only cents; non-negative outputs;
#       caller_charge - agent_payout == platform_fee for every valid input;
#       parse_curve roundtrip; fraction_for_rating bounded in [0, 1].
"""
from __future__ import annotations

import json

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from core.payments.base import (
    DEFAULT_FEE_BEARER_POLICY,
    VALID_FEE_BEARER_POLICIES,
    compute_platform_fee_cents,
    compute_success_distribution,
    normalize_fee_bearer_policy,
)
from core.payout_curve import (
    curve_to_json,
    fraction_for_rating,
    parse_curve,
    parse_curve_result,
)
from tests.strategies import cents

pytestmark = pytest.mark.property


# Use an int strategy compatible with the function's `int | None` signature.
fee_pct_int = st.integers(min_value=0, max_value=50)


# --- compute_platform_fee_cents ----------------------------------------------

@given(price=cents(), pct=fee_pct_int)
def test_fee_is_integer_and_non_negative(price, pct):
    fee = compute_platform_fee_cents(price, pct)
    assert isinstance(fee, int)
    assert fee >= 0


@given(price=cents(), pct=fee_pct_int)
def test_fee_does_not_exceed_price(price, pct):
    """A fee never exceeds the price — even at 50% the fee is half."""
    fee = compute_platform_fee_cents(price, pct)
    assert fee <= price


@given(price=cents())
def test_fee_zero_pct_gives_zero(price):
    """Edge: 0% fee always returns 0 regardless of price."""
    assert compute_platform_fee_cents(price, 0) == 0


@given(price=cents(), pct=fee_pct_int)
def test_fee_is_deterministic(price, pct):
    """Same inputs always give the same fee — no clocks or randomness."""
    a = compute_platform_fee_cents(price, pct)
    b = compute_platform_fee_cents(price, pct)
    assert a == b


@given(p1=cents(), p2=cents(), pct=fee_pct_int)
def test_fee_is_monotonic_in_price(p1, p2, pct):
    if p1 <= p2:
        assert compute_platform_fee_cents(p1, pct) <= compute_platform_fee_cents(p2, pct)


@given(price=cents())
def test_fee_negative_price_raises(price):
    with pytest.raises(ValueError):
        compute_platform_fee_cents(-1 - price, 10)


@given(price=cents())
def test_fee_negative_pct_raises(price):
    with pytest.raises(ValueError):
        compute_platform_fee_cents(price, -1)


# --- compute_success_distribution --------------------------------------------

policies = st.sampled_from(sorted(VALID_FEE_BEARER_POLICIES))


@given(price=cents(), pct=fee_pct_int, policy=policies)
def test_distribution_keys_present(price, pct, policy):
    d = compute_success_distribution(price, platform_fee_pct=pct, fee_bearer_policy=policy)
    assert set(d.keys()) >= {"caller_charge_cents", "agent_payout_cents", "platform_fee_cents"}


@given(price=cents(), pct=fee_pct_int, policy=policies)
def test_distribution_values_non_negative(price, pct, policy):
    d = compute_success_distribution(price, platform_fee_pct=pct, fee_bearer_policy=policy)
    for k in ("caller_charge_cents", "agent_payout_cents", "platform_fee_cents"):
        assert d[k] >= 0, f"{k}={d[k]}"


@given(price=cents(), pct=fee_pct_int, policy=policies)
def test_distribution_values_are_int(price, pct, policy):
    """Money path must be integer-only — no float drift."""
    d = compute_success_distribution(price, platform_fee_pct=pct, fee_bearer_policy=policy)
    for k in ("caller_charge_cents", "agent_payout_cents", "platform_fee_cents"):
        assert isinstance(d[k], int), f"{k} is {type(d[k]).__name__}"


@given(price=cents(), pct=fee_pct_int, policy=policies)
def test_distribution_conservation(price, pct, policy):
    """caller_charge - agent_payout == platform_fee — the function's docstring promise."""
    d = compute_success_distribution(price, platform_fee_pct=pct, fee_bearer_policy=policy)
    assert d["caller_charge_cents"] - d["agent_payout_cents"] == d["platform_fee_cents"]


@given(price=cents(), pct=fee_pct_int)
def test_caller_policy_pays_price_plus_fee(price, pct):
    d = compute_success_distribution(price, platform_fee_pct=pct, fee_bearer_policy="caller")
    fee = compute_platform_fee_cents(price, pct)
    assert d["caller_charge_cents"] == price + fee
    assert d["agent_payout_cents"] == price


@given(price=cents(), pct=fee_pct_int)
def test_worker_policy_caller_pays_only_price(price, pct):
    d = compute_success_distribution(price, platform_fee_pct=pct, fee_bearer_policy="worker")
    assert d["caller_charge_cents"] == price


@given(price=cents(), pct=fee_pct_int)
def test_distribution_is_deterministic(price, pct):
    a = compute_success_distribution(price, platform_fee_pct=pct, fee_bearer_policy="caller")
    b = compute_success_distribution(price, platform_fee_pct=pct, fee_bearer_policy="caller")
    assert a == b


# --- normalize_fee_bearer_policy ---------------------------------------------

@given(value=st.one_of(st.none(), st.text(max_size=20), policies))
def test_normalize_fee_bearer_returns_valid(value):
    out = normalize_fee_bearer_policy(value)
    assert out in VALID_FEE_BEARER_POLICIES


@given(value=st.text(max_size=20))
def test_normalize_unknown_falls_back_to_default(value):
    assume(value.strip().lower() not in VALID_FEE_BEARER_POLICIES)
    assert normalize_fee_bearer_policy(value) == DEFAULT_FEE_BEARER_POLICY


# --- payout curve ------------------------------------------------------------

valid_curve = st.dictionaries(
    keys=st.sampled_from(["1", "2", "3", "4", "5"]),
    values=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    max_size=5,
)


@given(curve=valid_curve)
def test_parse_curve_roundtrip(curve):
    """parse_curve(curve_to_json(c)) returns an equivalent dict."""
    raw = curve_to_json(curve) if curve else None
    if raw is None:
        assert parse_curve(raw) is None
    else:
        parsed = parse_curve(raw)
        assert parsed is not None
        assert {k: float(v) for k, v in parsed.items()} == {k: float(v) for k, v in curve.items()}


@given(curve=valid_curve, rating=st.integers(min_value=1, max_value=5))
def test_fraction_for_rating_bounded(curve, rating):
    f = fraction_for_rating(curve, rating)
    assert 0.0 <= f <= 1.0


@given(rating=st.integers(min_value=1, max_value=5))
def test_fraction_for_rating_none_curve_is_full_payout(rating):
    """No curve configured → full payout (fraction == 1.0)."""
    assert fraction_for_rating(None, rating) == 1.0
    assert fraction_for_rating({}, rating) == 1.0


@given(curve=valid_curve)
def test_curve_to_json_is_valid_json(curve):
    out = curve_to_json(curve) if curve else None
    if out is None:
        assert curve in (None, {})
    else:
        decoded = json.loads(out)
        assert isinstance(decoded, dict)


@given(bad_key=st.text(min_size=1, max_size=4).filter(lambda s: s.strip() not in {"1", "2", "3", "4", "5"}))
def test_parse_curve_rejects_bad_keys(bad_key):
    """Any star-rating key outside '1'..'5' (after strip) is rejected."""
    raw = json.dumps({bad_key: 0.5})
    r = parse_curve_result(raw)
    assert not r.ok


@given(value=st.floats(allow_nan=False, allow_infinity=False))
def test_parse_curve_rejects_out_of_range(value):
    assume(not (0.0 <= value <= 1.0))
    raw = json.dumps({"3": value})
    r = parse_curve_result(raw)
    assert not r.ok


@given(garbage=st.text(min_size=1, max_size=20))
def test_parse_curve_invalid_json_is_err(garbage):
    assume(not _is_parseable_curve(garbage))
    r = parse_curve_result(garbage)
    assert not r.ok


def _is_parseable_curve(raw: str) -> bool:
    try:
        decoded = json.loads(raw)
    except Exception:
        return False
    if not isinstance(decoded, dict):
        return False
    for k, v in decoded.items():
        if str(k).strip() not in {"1", "2", "3", "4", "5"}:
            return False
        try:
            f = float(v)
        except (TypeError, ValueError):
            return False
        if not (0.0 <= f <= 1.0):
            return False
    return True
