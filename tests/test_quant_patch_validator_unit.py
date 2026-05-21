"""Per-module unit tests for `agents.quant_patch_validator`.

# OWNS: fast tests (< 5s total) for every public function in every
#        module of the agent package — no LLM, no sandbox, no HTTP.
# NOT OWNS: the diff-oracle correctness matrix (see test_oracle.py),
#            signature LLM enrichment (see test_signature.py),
#            integration / lifecycle (see tests/integration/...).
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from agents.quant_patch_validator import (
    cluster as _cluster,
    coverage_track as _coverage,
    fuzz_atheris as _atheris,
    harness as _harness,
    report as _report,
    signature as _signature,
    triage as _triage,
)


@pytest.fixture(autouse=True)
def _disable_live_llm_triage(monkeypatch):
    monkeypatch.setattr(_triage, "_llm_triage_one", lambda *_args, **_kwargs: None)


# ---------------------------------------------------------------------------
# signature.py
# ---------------------------------------------------------------------------


def test_parse_signature_with_full_type_hints():
    src = "import numpy as np\ndef f(prices: np.ndarray, window: int) -> np.ndarray:\n    return prices\n"
    sig = _signature.parse_signature(src)
    assert sig is not None
    assert sig.function_name == "f"
    assert [p.type_name for p in sig.parameters] == ["ndarray", "int"]


def test_parse_signature_no_type_hints_uses_name_heuristic():
    src = "def f(prices, window):\n    return prices\n"
    sig = _signature.parse_signature(src)
    assert sig is not None
    assert [p.type_name for p in sig.parameters] == ["ndarray", "int"]


def test_parse_signature_picks_last_public_function():
    src = "def _helper(x): return x\ndef f(x): return x\ndef g(x): return x\n"
    sig = _signature.parse_signature(src)
    assert sig is not None and sig.function_name == "g"


def test_parse_signature_falls_back_to_private_when_only_private():
    src = "def _helper(x): return x\n"
    sig = _signature.parse_signature(src)
    assert sig is not None and sig.function_name == "_helper"


def test_parse_signature_returns_none_on_syntax_error():
    assert _signature.parse_signature("def f(:") is None


def test_parse_signature_returns_none_on_no_function():
    assert _signature.parse_signature("x = 1\ny = 2\n") is None
    assert _signature.parse_signature("class Foo: pass\n") is None


def test_parse_signature_captures_decorators():
    src = "from functools import lru_cache\n@lru_cache\ndef f(x): return x\n"
    sig = _signature.parse_signature(src)
    assert sig is not None
    assert any("lru_cache" in d for d in sig.decorators)


def test_parse_signature_handles_kwonly_args():
    src = "def f(x, *, period=14):\n    return x\n"
    sig = _signature.parse_signature(src)
    assert sig is not None
    kwonly = [p for p in sig.parameters if p.kw_only]
    assert len(kwonly) == 1 and kwonly[0].name == "period" and kwonly[0].has_default


def test_diff_signatures_name_mismatch_caught():
    ref = _signature.parse_signature("def f(x): return x")
    cand = _signature.parse_signature("def g(x): return x")
    pair = _signature.SignaturePair(reference=ref, candidate=cand, divergence={"kind": "function_name"})
    # We construct directly to assert SignaturePair structure
    assert pair.divergence is not None
    # And the higher-level infer_pair should detect it too:
    inferred = _signature.infer_pair("def f(x): return x", "def g(x): return x")
    assert inferred is not None
    assert inferred.divergence is not None
    assert inferred.divergence["kind"] == "function_name"


def test_diff_signatures_arity_mismatch_caught():
    inferred = _signature.infer_pair("def f(x): return x", "def f(x, y): return x")
    assert inferred is not None and inferred.divergence is not None
    assert inferred.divergence["kind"] == "positional_arity"


def test_diff_signatures_optional_kwarg_addition_tolerated():
    # adding an optional param is backward-compatible
    inferred = _signature.infer_pair(
        "def f(x): return x",
        "def f(x, opt=0): return x",
    )
    assert inferred is not None
    assert inferred.divergence is None


# ---------------------------------------------------------------------------
# harness.py — module isolation + safe call (oracle matrix lives elsewhere)
# ---------------------------------------------------------------------------


def test_safe_call_captures_exception():
    src = "def f(x):\n    raise ValueError('boom')\n"
    sig = _signature.parse_signature(src)
    h = _harness.Harness(src, src, sig)
    diff = h.call_both((1,), {})
    # Both sides raise the same exception → divergence_kind 'none'
    assert diff.divergence_kind == "none"
    assert diff.ref.exception_type == "ValueError"
    assert diff.cand.exception_type == "ValueError"


def test_safe_call_returns_value_on_success():
    src = "def f(x): return x * 2\n"
    sig = _signature.parse_signature(src)
    h = _harness.Harness(src, src, sig)
    diff = h.call_both((3,), {})
    assert diff.divergence_kind == "none"
    assert diff.ref.value == 6


def test_module_isolation_no_cross_pollution():
    ref = "G = 1\ndef f(x): return G + x\n"
    cand = "G = 100\ndef f(x): return G + x\n"
    sig = _signature.parse_signature(ref)
    h = _harness.Harness(ref, cand, sig)
    diff = h.call_both((5,), {})
    # Each side sees its own G; values differ → value divergence.
    assert diff.divergence_kind == "value"
    assert diff.ref.value == 6 and diff.cand.value == 105


# ---------------------------------------------------------------------------
# cluster.py
# ---------------------------------------------------------------------------


def _make_diff(kind: str, detail: dict, inputs_repr: str = "x=1") -> _harness.DiffRecord:
    return _harness.DiffRecord(
        inputs_repr=inputs_repr,
        ref=_harness.CallOutcome(value=None, exception_type=None, exception_msg=None),
        cand=_harness.CallOutcome(value=None, exception_type=None, exception_msg=None),
        divergence_kind=kind,
        divergence_detail=detail,
    )


def test_cluster_groups_same_exception_pair():
    diffs = [
        _harness.DiffRecord(
            inputs_repr=f"x={i}",
            ref=_harness.CallOutcome(value=None, exception_type="ValueError", exception_msg="a"),
            cand=_harness.CallOutcome(value=None, exception_type="TypeError", exception_msg="b"),
            divergence_kind="exception_mismatch",
            divergence_detail={"ref_exception": "ValueError", "cand_exception": "TypeError"},
        )
        for i in range(10)
    ]
    clusters = _cluster.cluster_divergences(diffs)
    assert len(clusters) == 1
    assert clusters[0].member_count == 10


def test_cluster_separates_different_magnitude_buckets():
    diffs = [
        _make_diff("value", {"max_abs_diff": 1e-3}, inputs_repr="small"),
        _make_diff("value", {"max_abs_diff": 1e6}, inputs_repr="big"),
    ]
    clusters = _cluster.cluster_divergences(diffs)
    assert len(clusters) == 2


def test_cluster_picks_smallest_representative():
    diffs = [
        _make_diff("value", {"max_abs_diff": 1e-3}, inputs_repr="long-input-string-here"),
        _make_diff("value", {"max_abs_diff": 1e-3}, inputs_repr="x"),
    ]
    clusters = _cluster.cluster_divergences(diffs)
    assert len(clusters) == 1
    assert clusters[0].representative.inputs_repr == "x"


def test_cluster_empty_input_returns_empty():
    assert _cluster.cluster_divergences([]) == []


def test_cluster_deterministic_ordering():
    diffs = [
        _make_diff("value", {"max_abs_diff": 1e-3}, inputs_repr=f"x={i}") for i in range(5)
    ]
    a = _cluster.cluster_divergences(diffs)
    b = _cluster.cluster_divergences(diffs)
    assert [c.cluster_id for c in a] == [c.cluster_id for c in b]


# ---------------------------------------------------------------------------
# triage.py — heuristic-only paths
# ---------------------------------------------------------------------------


def _cluster_with(kind: str, *, ref_raised=False, cand_raised=False, ref_exc=None, cand_exc=None, detail=None):
    rec = _harness.DiffRecord(
        inputs_repr="x",
        ref=_harness.CallOutcome(value=None, exception_type=ref_exc, exception_msg="r" if ref_raised else None),
        cand=_harness.CallOutcome(value=None, exception_type=cand_exc, exception_msg="c" if cand_raised else None),
        divergence_kind=kind,
        divergence_detail=detail or {},
    )
    return _cluster.DivergenceCluster(
        cluster_id="C001",
        divergence_kind=kind,
        member_count=1,
        representative=rec,
        signature=(kind,),
    )


def test_heuristic_exception_only_in_cand_is_regression():
    c = _cluster_with("exception_mismatch", cand_raised=True, cand_exc="ValueError")
    out = _triage.triage_clusters([c])
    assert out[0].verdict == _triage.VERDICT_REGRESSION
    assert out[0].triaged_by == "heuristic"


def test_heuristic_exception_only_in_ref_is_regression():
    c = _cluster_with("exception_mismatch", ref_raised=True, ref_exc="ValueError")
    out = _triage.triage_clusters([c])
    assert out[0].verdict == _triage.VERDICT_REGRESSION


def test_heuristic_both_raise_different_exceptions_is_both_wrong():
    c = _cluster_with(
        "exception_mismatch",
        ref_raised=True,
        cand_raised=True,
        ref_exc="ValueError",
        cand_exc="TypeError",
    )
    out = _triage.triage_clusters([c])
    assert out[0].verdict == _triage.VERDICT_BOTH_WRONG


def test_heuristic_shape_divergence_is_regression():
    c = _cluster_with("shape", detail={"ref_type": "ndarray", "cand_type": "dict"})
    out = _triage.triage_clusters([c])
    assert out[0].verdict == _triage.VERDICT_REGRESSION


def test_heuristic_value_divergence_is_regression():
    c = _cluster_with("value", detail={"max_abs_diff": 1.5})
    out = _triage.triage_clusters([c])
    assert out[0].verdict == _triage.VERDICT_REGRESSION


def test_triage_clusters_empty_returns_empty():
    assert _triage.triage_clusters([]) == []


# ---------------------------------------------------------------------------
# report.py — verdict computation
# ---------------------------------------------------------------------------


def test_verdict_summary_equivalent_when_no_clusters():
    out = _report.build_report(
        signature_pair=None, fuzz=None, clusters=[], triaged=[], tier_used="quick", spec_hint=None,
    )
    assert out["verdict"] == "equivalent"


def test_verdict_summary_regressions_found():
    rec = _cluster_with("value", detail={"max_abs_diff": 1.0})
    triaged = _triage.triage_clusters([rec])
    out = _report.build_report(
        signature_pair=None, fuzz=None, clusters=[rec], triaged=triaged,
        tier_used="quick", spec_hint=None,
    )
    assert out["verdict"] == "regressions_found"


def test_verdict_summary_contract_broken_when_shape_type_mismatch():
    rec = _cluster_with("shape", detail={"ref_type": "ndarray", "cand_type": "dict"})
    triaged = _triage.triage_clusters([rec])
    out = _report.build_report(
        signature_pair=None, fuzz=None, clusters=[rec], triaged=triaged,
        tier_used="quick", spec_hint=None,
    )
    assert out["verdict"] == "contract_broken"


def test_report_output_is_json_serialisable():
    out = _report.build_report(
        signature_pair=None, fuzz=None, clusters=[], triaged=[], tier_used="quick", spec_hint=None,
    )
    # Must round-trip without raising.
    json.loads(json.dumps(out, default=str))


def test_report_fuzz_stats_present_even_on_empty_clusters():
    out = _report.build_report(
        signature_pair=None, fuzz=None, clusters=[], triaged=[], tier_used="standard", spec_hint=None,
    )
    assert "fuzz_stats" in out
    assert out["fuzz_stats"]["tier_used"] == "standard"
    assert out["fuzz_stats"]["clusters"] == 0


# ---------------------------------------------------------------------------
# fuzz_atheris.py — availability + graceful fallback
# ---------------------------------------------------------------------------


def test_atheris_is_available_returns_bool():
    assert isinstance(_atheris.is_available(), bool)


def test_atheris_run_returns_empty_when_unavailable():
    if _atheris.is_available():
        pytest.skip("atheris IS available on this host; test inverts assumption")
    src = "def f(x): return x*2\n"
    sig = _signature.parse_signature(src)
    h = _harness.Harness(src, src, sig)
    result = _atheris.run_atheris_fuzz(h, {}, budget_seconds=1)
    assert result.inputs_explored == 0
    assert result.divergences == []


# ---------------------------------------------------------------------------
# coverage_track.py — graceful unavailable + tempfile cleanup
# ---------------------------------------------------------------------------


def test_coverage_disabled_when_coverage_py_missing(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "coverage":
            raise ImportError("simulated missing coverage")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with _coverage.candidate_coverage("def f(x): return x*2\n") as ctx:
        pass  # nothing to do; just verify start() didn't raise
    result = ctx.result()
    assert result.available is False
    assert result.coverage_pct is None


def test_coverage_tempfile_cleanup_after_use(tmp_path):
    # Run 5 sequential coverage sessions and assert no leftover qpv_cand_* in tempfile dir
    import glob
    import tempfile

    for _ in range(5):
        with _coverage.candidate_coverage("def f(x): return x*2\n") as ctx:
            pass
        ctx.result()  # invokes cleanup

    leftovers = glob.glob(f"{tempfile.gettempdir()}/qpv_cand_*.py")
    # Allow at most one from a concurrent session (unlikely in CI).
    assert len(leftovers) <= 1, f"too many leftover tempfiles: {leftovers}"
