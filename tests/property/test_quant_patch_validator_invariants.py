"""Hypothesis property tests for `quant_patch_validator`.

# OWNS: invariants that MUST hold across the input space — self-equivalence,
#        triage closure, schema closure, no-raw-exceptions-escape.
# NOT OWNS: oracle correctness matrix (see tests/test_quant_patch_validator_oracle.py),
#            specific bug reproductions (see corpus / scenarios tests).
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from agents.quant_patch_validator import signature as _signature
from agents.quant_patch_validator import triage as _triage
from agents.quant_patch_validator import run as validator_run


pytestmark = pytest.mark.property


@pytest.fixture(autouse=True)
def _disable_live_llm(monkeypatch):
    monkeypatch.setattr(_signature, "llm_enrich_constraints", lambda *_a, **_k: {})
    monkeypatch.setattr(_triage, "_llm_triage_one", lambda *_a, **_k: None)


_VALID_VERDICTS = frozenset(
    {
        "equivalent",
        "regressions_found",
        "contract_broken",
        "signature_divergence",
        "intended_changes_only",
    }
)

# A small catalogue of trivially-equivalent function bodies. The
# self-equivalence property runs each through `run({ref=X, cand=X})`
# and asserts `verdict == "equivalent"`.
_EQUIVALENT_BODIES = [
    "def f(x): return x\n",
    "def f(x): return x * 2\n",
    "def f(x, y): return x + y\n",
    "import numpy as np\ndef f(values): return float(np.asarray(values).sum())\n",
    "def f(prices, window):\n    if window <= 0: return None\n    return prices[:window]\n",
    "def f(): return 42\n",
]


# ---------------------------------------------------------------------------
# Self-equivalence — running the agent on (X, X) must always say equivalent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", _EQUIVALENT_BODIES)
def test_self_equivalence(body: str):
    out = validator_run(
        {
            "reference_code": body,
            "candidate_code": body,
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    # Must be 'equivalent' OR (less common) signature_divergence for
    # the empty-args case; never 'regressions_found' or 'contract_broken'.
    assert out["verdict"] in ("equivalent", "signature_divergence"), out


# ---------------------------------------------------------------------------
# Schema closure — verdict + fuzz_stats always present and well-formed
# ---------------------------------------------------------------------------


@settings(max_examples=20, deadline=None, suppress_health_check=list(HealthCheck))
@given(
    body=st.sampled_from(_EQUIVALENT_BODIES),
    budget=st.sampled_from(["quick"]),  # standard / deep too slow for property tests
    seconds=st.floats(min_value=1.0, max_value=3.0),
)
def test_output_schema_always_well_formed(body: str, budget: str, seconds: float):
    out = validator_run(
        {
            "reference_code": body,
            "candidate_code": body,
            "fuzz_budget": budget,
            "fuzz_seconds": seconds,
        }
    )
    # Either an error envelope or a full report.
    if "error" in out:
        assert isinstance(out["error"], dict)
        assert "code" in out["error"] and "message" in out["error"]
        return
    assert out["verdict"] in _VALID_VERDICTS, out["verdict"]
    assert "fuzz_stats" in out
    fs = out["fuzz_stats"]
    for k in ("tier_used", "inputs_explored", "divergences_found", "clusters"):
        assert k in fs, f"missing key in fuzz_stats: {k}"
    assert isinstance(out.get("confirmed_regressions"), list)
    assert isinstance(out.get("expected_divergences"), list)


# ---------------------------------------------------------------------------
# No raw exception escapes `run()` — even on adversarial payload dicts
# ---------------------------------------------------------------------------


_PAYLOAD_KEYS = st.sampled_from(
    [
        "reference_code", "candidate_code", "fuzz_budget", "fuzz_seconds",
        "rtol", "atol", "spec_hint", "fuzz_engine", "auto_tune_tolerance",
        "track_coverage", "signature_override", "_workspace_id",
        "garbage", "extra_field",
    ]
)
_PAYLOAD_VALUES = st.recursive(
    st.one_of(
        st.none(), st.booleans(), st.integers(), st.floats(allow_nan=True, allow_infinity=True),
        st.text(min_size=0, max_size=200), st.binary(min_size=0, max_size=100),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(min_size=1, max_size=10), children, max_size=5),
    ),
    max_leaves=15,
)


@settings(max_examples=50, deadline=None, suppress_health_check=list(HealthCheck))
@given(payload=st.dictionaries(_PAYLOAD_KEYS, _PAYLOAD_VALUES, max_size=8))
def test_run_never_raises(payload):
    """No matter how badly the payload is malformed, `run()` returns a dict."""
    out = validator_run(payload)
    assert isinstance(out, dict), f"run() returned non-dict: {type(out)}"
    # Either a valid report or a structured error envelope.
    assert ("verdict" in out) or ("error" in out)


# ---------------------------------------------------------------------------
# Triage closure — every cluster gets a known verdict
# ---------------------------------------------------------------------------


# Override the global pytest-timeout (60s) — this test allows up to 3 retries
# at the 30s ``quick`` budget, so the envelope can grow to ~120s legitimately.
# 180s gives headroom for the slow GH runner.
@pytest.mark.timeout(180)
def test_triage_closure_on_lookahead_bug():
    """Every cluster in a real regression must carry a known triage verdict."""
    ref = (
        "import numpy as np\n"
        "def rolling_mean(prices, window):\n"
        "    p = np.asarray(prices, dtype=np.float64)\n"
        "    out = np.full(p.shape, np.nan)\n"
        "    if window <= 0 or p.size < window:\n"
        "        return out\n"
        "    for i in range(window, p.size):\n"
        "        out[i] = p[i-window:i].mean()\n"
        "    return out\n"
    )
    cand = ref.replace("range(window, p.size)", "range(window-1, p.size)").replace(
        "p[i-window:i]", "p[i-window+1:i+1]"
    )
    # 2026-05-30: the search is time-budgeted and Hypothesis-driven, so
    # transient verdict='equivalent' happens on the slow GH runner.
    # Retry up to 3 times at the ``quick`` budget (30s) — each attempt
    # re-rolls Hypothesis's seed and interleaves with the time budget
    # differently, so a transient miss converges to a hit within 2-3
    # tries. ``regressions_found`` is the only correct verdict for this
    # input (the candidate has a one-step lookahead bug); ``equivalent``
    # is never accepted as a flaky pass. The pytest-timeout default of
    # 60s isn't enough for the retry envelope, so override per-test.
    out = None
    for _attempt in range(3):
        out = validator_run(
            {
                "reference_code": ref,
                "candidate_code": cand,
                "fuzz_budget": "quick",
            }
        )
        if out.get("verdict") == "regressions_found":
            break
    assert out is not None and out["verdict"] == "regressions_found", (
        f"validator did not find the lookahead-bug regression after 3 attempts "
        f"(last verdict={(out or {}).get('verdict')!r})"
    )
    valid_cluster_verdicts = {"regression", "expected", "both_wrong"}
    for cluster in out["confirmed_regressions"] + out["expected_divergences"]:
        assert cluster["verdict"] in valid_cluster_verdicts, cluster["verdict"]
