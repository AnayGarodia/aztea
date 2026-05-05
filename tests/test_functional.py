"""Tests for core/functional.py — Result monad, pipe, and pure math helpers."""

import pytest

from core.functional import (
    Ok,
    Err,
    pipe,
    compose,
    pipeline,
    validate,
    require_positive_int,
    require_non_negative_int,
    compute_platform_fee,
    compute_agent_payout,
    compute_caller_refund,
    compute_partial_refund,
)


# ---------------------------------------------------------------------------
# Ok / Err construction and basic properties
# ---------------------------------------------------------------------------


def test_ok_is_truthy():
    assert Ok(42)


def test_err_is_falsy():
    assert not Err("bad")


def test_ok_bool_field():
    assert Ok(1).ok is True


def test_err_bool_field():
    assert Err("x").ok is False


def test_ok_unwrap():
    assert Ok("hello").unwrap() == "hello"


def test_err_unwrap_raises():
    with pytest.raises(ValueError, match="Called unwrap"):
        Err("oops").unwrap()


def test_ok_unwrap_or():
    assert Ok(5).unwrap_or(99) == 5


def test_err_unwrap_or():
    assert Err("fail").unwrap_or(99) == 99


# ---------------------------------------------------------------------------
# map / map_err / and_then
# ---------------------------------------------------------------------------


def test_ok_map():
    result = Ok(3).map(lambda x: x * 2)
    assert result == Ok(6)


def test_err_map_is_noop():
    e = Err("boom")
    assert e.map(lambda x: x * 2) is e


def test_ok_map_err_is_noop():
    o = Ok(3)
    assert o.map_err(str) is o


def test_err_map_err():
    result = Err(42).map_err(lambda e: f"error:{e}")
    assert result == Err("error:42")


def test_ok_and_then_propagates():
    result = Ok(5).and_then(lambda x: Ok(x + 1))
    assert result == Ok(6)


def test_ok_and_then_can_fail():
    result = Ok(5).and_then(lambda x: Err("too big") if x > 3 else Ok(x))
    assert result == Err("too big")


def test_err_and_then_is_noop():
    e = Err("gone")
    assert e.and_then(lambda x: Ok(x)) is e


# ---------------------------------------------------------------------------
# pipe
# ---------------------------------------------------------------------------


def test_pipe_single_fn():
    assert pipe(3, lambda x: x + 1) == 4


def test_pipe_chain():
    assert pipe(1, lambda x: x + 1, lambda x: x * 2, lambda x: x - 1) == 3


def test_pipe_short_circuits_on_err():
    called = []

    def fail(_):
        return Err("stop")

    def side_effect(x):
        called.append(x)
        return x

    result = pipe(10, fail, side_effect)
    assert isinstance(result, Err)
    assert called == []


def test_pipe_passes_ok_through():
    result = pipe(Ok(3), lambda r: r.map(lambda x: x * 2))
    assert result == Ok(6)


# ---------------------------------------------------------------------------
# compose / pipeline
# ---------------------------------------------------------------------------


def test_compose_right_to_left():
    def double(x): return x * 2
    def inc(x): return x + 1
    # compose(double, inc)(3) == double(inc(3)) == double(4) == 8
    assert compose(double, inc)(3) == 8


def test_pipeline_left_to_right():
    def double(x): return x * 2
    def inc(x): return x + 1
    # pipeline(double, inc)(3) == inc(double(3)) == inc(6) == 7
    assert pipeline(double, inc)(3) == 7


# ---------------------------------------------------------------------------
# validate helpers
# ---------------------------------------------------------------------------


def test_validate_passes():
    assert validate(5, lambda x: x > 0, "must be positive") == Ok(5)


def test_validate_fails():
    assert validate(-1, lambda x: x > 0, "must be positive") == Err("must be positive")


def test_require_positive_int_ok():
    assert require_positive_int(1, "amount") == Ok(1)


def test_require_positive_int_rejects_zero():
    result = require_positive_int(0, "amount")
    assert isinstance(result, Err)
    assert "positive" in result.error


def test_require_positive_int_rejects_negative():
    result = require_positive_int(-1, "amount")
    assert isinstance(result, Err)


def test_require_positive_int_rejects_float():
    result = require_positive_int(1.5, "amount")
    assert isinstance(result, Err)
    assert "integer" in result.error


def test_require_positive_int_rejects_bool():
    result = require_positive_int(True, "amount")
    assert isinstance(result, Err)


def test_require_non_negative_int_allows_zero():
    assert require_non_negative_int(0, "fee") == Ok(0)


def test_require_non_negative_int_rejects_negative():
    result = require_non_negative_int(-1, "fee")
    assert isinstance(result, Err)


# ---------------------------------------------------------------------------
# Pure settlement math
# ---------------------------------------------------------------------------


def test_platform_fee_10_pct():
    assert compute_platform_fee(1000, 10) == 100


def test_platform_fee_truncates():
    # 1001 cents * 10% = 100.1 → truncated to 100
    assert compute_platform_fee(1001, 10) == 100


def test_agent_payout():
    assert compute_agent_payout(1000, 10) == 900


def test_agent_payout_zero_fee():
    assert compute_agent_payout(500, 0) == 500


def test_caller_refund_returns_full_charge():
    assert compute_caller_refund(800, 200) == 800


def test_partial_refund_full_quality():
    # quality 100 → full payout, no refund
    assert compute_partial_refund(1000, 1000, 100) == 0


def test_partial_refund_zero_quality():
    # quality 0 → full refund
    assert compute_partial_refund(1000, 1000, 0) == 1000


def test_partial_refund_half_quality():
    # caller paid 1000, price 1000, quality 50 → effective 500 → refund 500
    assert compute_partial_refund(1000, 1000, 50) == 500


def test_partial_refund_clamps_quality_above_100():
    # quality 150 treated as 100 → no refund
    assert compute_partial_refund(1000, 1000, 150) == 0


def test_partial_refund_clamps_quality_below_0():
    # quality -10 treated as 0 → full refund
    assert compute_partial_refund(1000, 1000, -10) == 1000


def test_partial_refund_never_negative():
    # If caller was undercharged vs effective price, refund stays 0
    assert compute_partial_refund(200, 1000, 100) == 0


# ---------------------------------------------------------------------------
# compute_clawback_cents
# ---------------------------------------------------------------------------

from core.functional import compute_clawback_cents  # noqa: E402


def test_clawback_full_payout_no_clawback():
    assert compute_clawback_cents(1000, 1.0) == 0


def test_clawback_above_1_treated_as_full():
    assert compute_clawback_cents(1000, 1.5) == 0


def test_clawback_zero_payout_full_clawback():
    assert compute_clawback_cents(1000, 0.0) == 1000


def test_clawback_half():
    assert compute_clawback_cents(100, 0.5) == 50


def test_clawback_rounds_half_up():
    # 101 * (1 - 0.5) = 50.5 → rounds up to 51
    assert compute_clawback_cents(101, 0.5) == 51


def test_clawback_negative_fraction_treated_as_zero():
    assert compute_clawback_cents(100, -0.1) == 100


def test_clawback_zero_payout_cents():
    assert compute_clawback_cents(0, 0.5) == 0


# ---------------------------------------------------------------------------
# parse_curve_result (Result-returning variant)
# ---------------------------------------------------------------------------

from core.payout_curve import parse_curve_result  # noqa: E402
from core.functional import Ok, Err  # noqa: E402 (already imported above but repeated for clarity)


def test_parse_curve_result_none_returns_empty_ok():
    r = parse_curve_result(None)
    assert isinstance(r, Ok)
    assert r.value == {}


def test_parse_curve_result_valid_dict():
    r = parse_curve_result({"1": 0.0, "5": 1.0})
    assert isinstance(r, Ok)
    assert r.value == {"1": 0.0, "5": 1.0}


def test_parse_curve_result_valid_json_string():
    r = parse_curve_result('{"3": 0.5}')
    assert isinstance(r, Ok)
    assert r.value == {"3": 0.5}


def test_parse_curve_result_invalid_json():
    r = parse_curve_result("{bad}")
    assert isinstance(r, Err)
    assert "valid JSON" in r.error


def test_parse_curve_result_invalid_star_key():
    r = parse_curve_result({"6": 0.5})
    assert isinstance(r, Err)
    assert "1" in r.error and "5" in r.error


def test_parse_curve_result_fraction_out_of_range():
    r = parse_curve_result({"3": 1.5})
    assert isinstance(r, Err)
    assert "0.0" in r.error and "1.0" in r.error


def test_parse_curve_result_non_numeric_fraction():
    r = parse_curve_result({"3": "bad"})
    assert isinstance(r, Err)
    assert "number" in r.error


def test_parse_curve_result_non_dict_input():
    r = parse_curve_result([1, 2, 3])
    assert isinstance(r, Err)
    assert "JSON object" in r.error


def test_parse_curve_backward_compat_still_raises():
    """parse_curve (legacy) must still raise ValueError — no silent behaviour change."""
    from core.payout_curve import parse_curve
    import pytest

    with pytest.raises(ValueError, match="valid JSON"):
        parse_curve("{bad}")
