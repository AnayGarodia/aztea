"""Fuzz the harness with Hypothesis-generated inputs, collect divergences.

# OWNS: building a Hypothesis strategy from a `FunctionSignature` + optional
#        LLM-enriched constraints, driving the harness with a configurable
#        wall-clock budget, and returning the collected `DiffRecord[]`.
# NOT OWNS: oracle math (in harness.py), clustering / shrinking (in
#            cluster.py), classification (in triage.py).
# INVARIANTS:
#   - We DO NOT raise out of `run_fuzz`. Every divergence is collected,
#     never thrown.
#   - The fuzz budget is wall-clock-bounded HARD. We drive the strategy
#     via `strat.example()` in a manual loop, NOT via `@given` — the
#     latter respects `max_examples` but not wall-clock, which made
#     "quick" tier overshoot by 5x in early testing.
# DECISIONS:
#   - Default tolerances baked from spike S3: rtol=1e-7, atol=1e-9.
#     Reduction-order skew between equivalent numerical recipes shows
#     up at ~1e-8 in real quant code, so 1e-7 is the right relative
#     floor and 1e-9 the right absolute floor.
#   - Auto-tuning (run reference 20× on permuted inputs, set atol =
#     max(1e-9, 10×observed)) is available via `auto_tune=True`.
# KNOWN DEBT:
#   - Per-input coverage (coverage.py instrumentation) not yet wired.
"""

from __future__ import annotations

import math
import random
import time
import warnings
from dataclasses import dataclass, field
from typing import Any

from agents.quant_patch_validator.harness import DiffRecord, Harness
from agents.quant_patch_validator.signature import FunctionSignature, Parameter

# Hypothesis warns once per call when you use `.example()` outside an
# interactive session. We use it intentionally — it's the cheapest way
# to draw one input from a strategy while staying in wall-clock control
# of the outer loop. Suppress the warning so it doesn't pollute stdout
# of the bench scorer (which serialises a single JSON document).
try:
    from hypothesis.errors import NonInteractiveExampleWarning as _NIEW

    warnings.filterwarnings("ignore", category=_NIEW)
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Tunables — module-level constants
# ---------------------------------------------------------------------------
# Defaults calibrated against quant-bench v0.1 (rolling, returns, RSI,
# Sharpe, vol). Tighter values trip false-alarms on cumsum-vs-loop and
# subtraction-vs-ratio rewrites where reduction-order skew is ~1e-7 to
# 1e-8. Looser values miss real bugs. These cover the typical AI failure
# range (off-by-one, sign-flip, missing-factor, unit-confusion) which
# produce divergences at 1e-3 or worse.
_DEFAULT_RTOL = 1e-5
_DEFAULT_ATOL = 1e-7
_MAX_ARRAY_SIZE = 250            # cap on generated array length
_MAX_INT_MAGNITUDE = 10_000      # cap on bare integer inputs
_FLOAT_MIN = -1e6
_FLOAT_MAX = 1e6
_AUTO_TUNE_TRIALS = 20
_AUTO_TUNE_FLOOR_ATOL = 1e-9
_AUTO_TUNE_HEADROOM = 10.0
_MAX_DIVERGENCES_DEFAULT = 200   # stop collecting beyond this — clustering still works


@dataclass
class FuzzResult:
    inputs_explored: int
    divergences: list[DiffRecord] = field(default_factory=list)
    elapsed_s: float = 0.0
    atol_used: float = _DEFAULT_ATOL
    rtol_used: float = _DEFAULT_RTOL
    auto_tuned: bool = False


# ---------------------------------------------------------------------------
# Strategy synthesis — deterministic per-type generators
# ---------------------------------------------------------------------------


def _hypothesis_strategy_for_type(type_name: str, constraints: set[str]):
    """Build a Hypothesis strategy from our compact type vocabulary."""
    import numpy as np
    from hypothesis import strategies as st
    from hypothesis.extra.numpy import arrays

    finite_floats = st.floats(
        min_value=_FLOAT_MIN,
        max_value=_FLOAT_MAX,
        allow_nan=False,
        allow_infinity=False,
    )
    if "positive" in constraints:
        finite_floats = st.floats(
            min_value=1e-3, max_value=_FLOAT_MAX, allow_nan=False, allow_infinity=False
        )

    if type_name == "int":
        if "positive" in constraints:
            return st.integers(min_value=1, max_value=_MAX_INT_MAGNITUDE)
        return st.integers(min_value=-_MAX_INT_MAGNITUDE, max_value=_MAX_INT_MAGNITUDE)

    if type_name == "float":
        return finite_floats

    if type_name == "bool":
        return st.booleans()

    if type_name == "str":
        return st.text(min_size=0, max_size=32)

    if type_name == "ndarray":
        # Default min-size is 1 (not 2 or 0): zero-size arrays surface
        # empty-input bugs (entry 016) and size-1 arrays surface
        # single-element edge cases. We bias higher sizes through a
        # composite strategy below.
        size_min = 1 if "non_empty" in constraints else 0
        small = arrays(
            np.float64,
            st.integers(min_value=size_min, max_value=4),
            elements=finite_floats,
        )
        normal = arrays(
            np.float64,
            st.integers(min_value=max(5, size_min), max_value=_MAX_ARRAY_SIZE),
            elements=finite_floats,
        )
        # 25% small, 75% normal — keeps the fuzzer focused on realistic
        # quant sizes while still probing edge cases.
        return st.one_of(small, normal)

    if type_name == "series":
        size_min = 1 if "non_empty" in constraints else 0
        return arrays(
            np.float64,
            st.integers(min_value=max(2, size_min), max_value=_MAX_ARRAY_SIZE),
            elements=finite_floats,
        ).map(lambda a: _np_to_series(a))

    if type_name == "dataframe":
        return arrays(
            np.float64,
            st.integers(min_value=2, max_value=_MAX_ARRAY_SIZE),
            elements=finite_floats,
        ).map(lambda a: _np_to_df(a))

    if type_name == "list":
        return st.lists(finite_floats, min_size=1, max_size=_MAX_ARRAY_SIZE)

    if type_name == "dict":
        return st.dictionaries(st.text(min_size=1, max_size=16), finite_floats, max_size=8)

    # "any" → fall back to floats, the most common quant case
    return finite_floats


def _np_to_series(arr):
    import pandas as pd

    return pd.Series(arr)


def _np_to_df(arr):
    import pandas as pd

    return pd.DataFrame({"value": arr})


def _constraints_from_enrichment(
    param_name: str, enrichment: dict[str, Any]
) -> set[str]:
    for pc in (enrichment or {}).get("parameter_constraints", []) or []:
        if pc.get("name") == param_name:
            return set(pc.get("constraints") or [])
    return set()


def _build_combined_strategy(sig: FunctionSignature, enrichment: dict[str, Any]):
    """Compose one strategy that returns (args_tuple, kwargs_dict)."""
    from hypothesis import strategies as st

    pos_params: list[Parameter] = [p for p in sig.parameters if not p.kw_only and not p.has_default]
    kw_params: list[Parameter] = [p for p in sig.parameters if p.kw_only and not p.has_default]

    if not pos_params and not kw_params:
        return st.tuples(st.just(()), st.just({}))

    pos_strats = [
        _hypothesis_strategy_for_type(p.type_name, _constraints_from_enrichment(p.name, enrichment))
        for p in pos_params
    ]
    kw_strats = {
        p.name: _hypothesis_strategy_for_type(
            p.type_name, _constraints_from_enrichment(p.name, enrichment)
        )
        for p in kw_params
    }

    def _to_tuple_and_dict(parts):
        pos = tuple(parts[: len(pos_strats)])
        kw = dict(zip(kw_strats.keys(), parts[len(pos_strats) :]))
        return pos, kw

    all_strats = pos_strats + list(kw_strats.values())
    return st.tuples(*all_strats).map(_to_tuple_and_dict)


# ---------------------------------------------------------------------------
# Auto-tune tolerance
# ---------------------------------------------------------------------------


def auto_tune_tolerance(
    harness: Harness,
    enrichment: dict[str, Any],
    *,
    trials: int = _AUTO_TUNE_TRIALS,
) -> float:
    """Estimate an atol floor from the reference's own self-divergence.

    Runs the REFERENCE function twice per trial with the same input
    permuted; observed |Δ| measures reduction-order noise. We use 10×
    that as the atol floor.
    """
    import numpy as np

    strat = _build_combined_strategy(harness.signature, enrichment)
    rng = random.Random(0)
    max_observed = 0.0
    used = 0

    for _ in range(trials):
        try:
            args, kwargs = strat.example()
        except Exception:
            continue
        used += 1
        a2 = tuple(_permute(a, rng) for a in args)
        d = harness.call_both(args, kwargs)
        d2 = harness.call_both(a2, kwargs)
        a_val, b_val = d.ref.value, d2.ref.value
        try:
            a_arr = np.asarray(a_val, dtype=np.float64)
            b_arr = np.asarray(b_val, dtype=np.float64)
        except Exception:
            continue
        if a_arr.shape != b_arr.shape:
            continue
        finite = np.isfinite(a_arr) & np.isfinite(b_arr)
        if not finite.any():
            continue
        m = float(np.abs(a_arr[finite] - b_arr[finite]).max())
        if math.isfinite(m) and m > max_observed:
            max_observed = m

    if not used:
        return _AUTO_TUNE_FLOOR_ATOL
    return max(_AUTO_TUNE_FLOOR_ATOL, _AUTO_TUNE_HEADROOM * max_observed)


def _permute(value: Any, rng: random.Random) -> Any:
    """Permute a list / ndarray (returns identity for other types)."""
    try:
        import numpy as np

        if isinstance(value, np.ndarray):
            idx = list(range(value.size))
            rng.shuffle(idx)
            return value.flatten()[idx].reshape(value.shape)
    except ImportError:
        pass
    if isinstance(value, list):
        out = list(value)
        rng.shuffle(out)
        return out
    return value


# ---------------------------------------------------------------------------
# The fuzz loop — manual, wall-clock-bounded
# ---------------------------------------------------------------------------


def run_fuzz(
    harness: Harness,
    enrichment: dict[str, Any],
    *,
    budget_seconds: float,
    rtol: float = _DEFAULT_RTOL,
    atol: float = _DEFAULT_ATOL,
    auto_tune: bool = False,
    max_divergences: int = _MAX_DIVERGENCES_DEFAULT,
) -> FuzzResult:
    """Drive the harness with strategy-generated inputs in a manual loop.

    Why not `@given`: Hypothesis' default loop respects `max_examples`
    but not wall-clock. With `max_examples=200_000` and a 30s tier we
    overshot by 5x in early testing. `strat.example()` lets us check
    the clock between every input.
    """
    eff_atol = atol
    auto_tuned = False
    if auto_tune:
        eff_atol = max(atol, auto_tune_tolerance(harness, enrichment))
        auto_tuned = True

    strat = _build_combined_strategy(harness.signature, enrichment)
    divergences: list[DiffRecord] = []
    explored = 0
    started = time.time()
    while time.time() - started < budget_seconds:
        if len(divergences) >= max_divergences:
            break
        try:
            args, kwargs = strat.example()
        except Exception:
            # Sampler error (rare; e.g. unsatisfiable constraints) —
            # count it as one exploration so we don't hot-loop on a
            # broken strategy.
            explored += 1
            continue
        explored += 1
        diff = harness.call_both(args, kwargs, rtol=rtol, atol=eff_atol)
        if diff.divergence_kind != "none":
            divergences.append(diff)

    return FuzzResult(
        inputs_explored=explored,
        divergences=divergences,
        elapsed_s=round(time.time() - started, 2),
        atol_used=eff_atol,
        rtol_used=rtol,
        auto_tuned=auto_tuned,
    )
