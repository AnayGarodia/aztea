"""Diff-oracle correctness matrix.

# OWNS: table-driven coverage of `harness._classify_divergence` and
#        `harness._compare_values` across every (ref, cand, expected)
#        case we care about. ANY regression in the oracle should fail
#        at least one row here.
# NOT OWNS: end-to-end agent calls (see lifecycle / corpus tests).
# DECISIONS:
#   - We test the harness oracle directly via two trivial modules that
#     each return whatever the test passes in. This isolates the oracle
#     from signature inference, fuzz strategy, clustering, etc.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import pytest

from agents.quant_patch_validator import harness as _harness, signature as _signature


# Trivial harness: both impls just return whatever was passed in. The
# fuzzer-facing infrastructure isn't exercised here — we drive the
# oracle directly via `call_both`.
_PASS_THROUGH = "def f(x): return x\n"


@dataclass(frozen=True)
class OracleCase:
    name: str
    ref: Any
    cand: Any
    expected_kind: str  # 'none' | 'value' | 'shape' | 'exception_mismatch'
    expected_detail_reason: str | None = None  # specific 'reason' field, if any
    rtol: float = 1e-5
    atol: float = 1e-7


# ---------------------------------------------------------------------------
# Build the case matrix
# ---------------------------------------------------------------------------

_INF = float("inf")
_NAN = float("nan")

SCALAR_CASES = [
    OracleCase("scalar_float_equal", 1.0, 1.0, "none"),
    OracleCase("scalar_float_within_rtol", 1.0, 1.0 + 1e-7, "none"),
    OracleCase("scalar_float_outside_rtol", 1.0, 1.0 + 1e-3, "value"),
    OracleCase("scalar_both_nan", _NAN, _NAN, "none"),
    OracleCase("scalar_nan_vs_finite", _NAN, 1.0, "value"),
    OracleCase("scalar_inf_same_sign", _INF, _INF, "none"),
    OracleCase("scalar_neg_inf_same_sign", -_INF, -_INF, "none"),
    OracleCase("scalar_inf_opposite_sign", _INF, -_INF, "value", "inf_sign_mismatch"),
    OracleCase("scalar_inf_vs_finite", _INF, 1.0, "value", "inf_finite_mismatch"),
    OracleCase("scalar_subnormal_within_atol", 1e-30, 1e-30 + 1e-50, "none"),
    OracleCase("scalar_exactly_zero", 0.0, 0.0, "none"),
    OracleCase("scalar_int_equal", 5, 5, "none"),
    OracleCase("scalar_int_off_by_one", 5, 6, "value"),
    OracleCase("scalar_int_vs_np_int64", 5, np.int64(5), "none"),
    OracleCase("scalar_float_vs_np_float64", 1.5, np.float64(1.5), "none"),
    OracleCase("scalar_bool_vs_np_bool", True, np.bool_(True), "none"),
]

ARRAY_CASES = [
    OracleCase("arr_identical", np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0, 3.0]), "none"),
    OracleCase(
        "arr_within_rtol",
        np.array([1.0, 2.0, 3.0]),
        np.array([1.0 + 1e-9, 2.0, 3.0]),
        "none",
    ),
    OracleCase(
        "arr_outside_rtol",
        np.array([1.0, 2.0, 3.0]),
        np.array([1.5, 2.0, 3.0]),
        "value",
    ),
    OracleCase(
        "arr_matching_nan_pattern",
        np.array([1.0, _NAN, 3.0]),
        np.array([1.0, _NAN, 3.0]),
        "none",
    ),
    OracleCase(
        "arr_mismatched_nan_pattern",
        np.array([1.0, _NAN, 3.0]),
        np.array([1.0, 2.0, 3.0]),
        "value",
        "nan_pattern_mismatch",
    ),
    OracleCase(
        "arr_matching_inf_same_sign",
        np.array([1.0, _INF, 3.0]),
        np.array([1.0, _INF, 3.0]),
        "none",
    ),
    OracleCase(
        "arr_mismatched_inf_pattern",
        np.array([1.0, _INF, 3.0]),
        np.array([1.0, 999.0, 3.0]),
        "value",
        "inf_pattern_mismatch",
    ),
    OracleCase(
        "arr_inf_opposite_sign",
        np.array([1.0, _INF, 3.0]),
        np.array([1.0, -_INF, 3.0]),
        "value",
        "inf_sign_mismatch",
    ),
    OracleCase(
        "arr_shape_mismatch",
        np.array([1.0, 2.0, 3.0]),
        np.array([1.0, 2.0, 3.0, 4.0]),
        "shape",
    ),
    OracleCase(
        "arr_different_ndim",
        np.array([1.0, 2.0, 3.0, 4.0]),
        np.array([[1.0, 2.0], [3.0, 4.0]]),
        "shape",
    ),
    OracleCase("arr_empty_both", np.array([]), np.array([]), "none"),
    OracleCase("arr_all_nan_both", np.array([_NAN, _NAN]), np.array([_NAN, _NAN]), "none"),
    OracleCase(
        "arr_one_diverging_at_end",
        np.arange(1000, dtype=np.float64),
        np.concatenate([np.arange(999, dtype=np.float64), np.array([99999.0])]),
        "value",
    ),
]

CONTAINER_CASES = [
    # ndarray vs list with same numeric content → equivalent (our tolerance)
    OracleCase("list_vs_ndarray_same", [1.0, 2.0, 3.0], np.array([1.0, 2.0, 3.0]), "none"),
    # tuple is NOT array-like for our oracle (intentional — entry 003 tuple-broken-sig case)
    OracleCase("tuple_vs_ndarray", (1.0, 2.0, 3.0), np.array([1.0, 2.0, 3.0]), "shape"),
    # ndarray vs dict → shape divergence
    OracleCase("ndarray_vs_dict", np.array([1.0]), {"a": 1.0}, "shape"),
    # dict vs dict equal → equivalent (falls into the "non_numeric" equality branch)
    OracleCase("dict_equal", {"a": 1}, {"a": 1}, "none"),
    OracleCase("dict_different_keys", {"a": 1}, {"b": 1}, "value"),
]

PANDAS_CASES = [
    OracleCase(
        "series_equal",
        pd.Series([1.0, 2.0, 3.0]),
        pd.Series([1.0, 2.0, 3.0]),
        "none",
    ),
    OracleCase(
        "series_with_nan_matching",
        pd.Series([1.0, _NAN, 3.0]),
        pd.Series([1.0, _NAN, 3.0]),
        "none",
    ),
    OracleCase(
        "series_diff_values",
        pd.Series([1.0, 2.0, 3.0]),
        pd.Series([1.0, 99.0, 3.0]),
        "value",
    ),
]

ALL_CASES = SCALAR_CASES + ARRAY_CASES + CONTAINER_CASES + PANDAS_CASES


# ---------------------------------------------------------------------------
# Direct oracle test (bypass module exec, drive harness directly)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.name)
def test_oracle_classification(case: OracleCase):
    """One row per (ref, cand) pair; assert kind + optional reason match."""
    # Build a harness whose ref/cand functions return constants captured
    # in closures. We can't exec a closure into a fresh namespace, so we
    # poke the harness's internals directly with mock CallOutcome values.
    ref_outcome = _harness.CallOutcome(value=case.ref, exception_type=None, exception_msg=None)
    cand_outcome = _harness.CallOutcome(value=case.cand, exception_type=None, exception_msg=None)
    kind, detail = _harness._classify_divergence(
        ref_outcome, cand_outcome, rtol=case.rtol, atol=case.atol
    )
    assert kind == case.expected_kind, (
        f"{case.name}: expected kind={case.expected_kind}, got {kind} "
        f"(detail={detail})"
    )
    if case.expected_detail_reason is not None:
        assert detail is not None and detail.get("reason") == case.expected_detail_reason


# ---------------------------------------------------------------------------
# Exception-side oracle cases
# ---------------------------------------------------------------------------


def _outcome(value=None, exc_type: str | None = None, exc_msg: str | None = None):
    return _harness.CallOutcome(value=value, exception_type=exc_type, exception_msg=exc_msg)


def test_both_raise_same_type_is_equivalent():
    kind, _ = _harness._classify_divergence(
        _outcome(exc_type="ValueError", exc_msg="x"),
        _outcome(exc_type="ValueError", exc_msg="x"),
        rtol=1e-5,
        atol=1e-7,
    )
    assert kind == "none"


def test_both_raise_different_types_is_exception_mismatch():
    kind, _ = _harness._classify_divergence(
        _outcome(exc_type="ValueError", exc_msg="x"),
        _outcome(exc_type="TypeError", exc_msg="x"),
        rtol=1e-5,
        atol=1e-7,
    )
    assert kind == "exception_mismatch"


def test_only_one_raises_is_exception_mismatch():
    kind, _ = _harness._classify_divergence(
        _outcome(value=1.0),
        _outcome(exc_type="ValueError", exc_msg="x"),
        rtol=1e-5,
        atol=1e-7,
    )
    assert kind == "exception_mismatch"


# ---------------------------------------------------------------------------
# Real harness round-trip — sanity that the integration path matches the
# direct oracle path on a few representative cases
# ---------------------------------------------------------------------------


def test_harness_roundtrip_passthrough_equivalent():
    """Both sides return the same value via passthrough → equivalent."""
    sig = _signature.parse_signature(_PASS_THROUGH)
    h = _harness.Harness(_PASS_THROUGH, _PASS_THROUGH, sig)
    diff = h.call_both((42,), {})
    assert diff.divergence_kind == "none"


def test_harness_roundtrip_value_divergence():
    """Distinct return values via two different lambdas → value divergence."""
    ref = "def f(x): return x + 1\n"
    cand = "def f(x): return x + 2\n"
    sig = _signature.parse_signature(ref)
    h = _harness.Harness(ref, cand, sig)
    diff = h.call_both((10,), {})
    assert diff.divergence_kind == "value"
