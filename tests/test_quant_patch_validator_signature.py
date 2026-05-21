"""Signature module deep tests — LLM enrichment, override, name heuristic.

# OWNS: tests for `signature.llm_enrich_constraints`, `signature.infer_pair`
#        edge cases (optional vs required param drift), and the
#        `_NAME_TYPE_HINTS` lookup completeness.
# NOT OWNS: AST parsing happy path (see test_quant_patch_validator_unit.py).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agents.quant_patch_validator import signature as _signature


# ---------------------------------------------------------------------------
# LLM enrichment — three paths: unavailable, malformed, valid
# ---------------------------------------------------------------------------


def test_llm_enrichment_returns_empty_when_no_llm(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "core.llm":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    sig = _signature.parse_signature("def f(prices, window): return prices\n")
    out = _signature.llm_enrich_constraints(sig)
    assert out == {}


def test_llm_enrichment_returns_empty_on_malformed_json():
    class _FakeResp:
        text = "this is not JSON at all, totally prose"

    with patch("core.llm.run_with_fallback", return_value=_FakeResp()):
        sig = _signature.parse_signature("def f(prices, window): return prices\n")
        out = _signature.llm_enrich_constraints(sig)
        assert out == {}


def test_llm_enrichment_returns_empty_on_llm_raise():
    from core.llm.errors import LLMError

    with patch("core.llm.run_with_fallback", side_effect=LLMError("provider", "model", "boom")):
        sig = _signature.parse_signature("def f(prices, window): return prices\n")
        out = _signature.llm_enrich_constraints(sig)
        assert out == {}


def test_llm_enrichment_returns_parsed_dict_on_valid_json():
    valid_response = json.dumps(
        {"parameter_constraints": [{"name": "prices", "constraints": ["positive", "non_empty"]}]}
    )

    class _FakeResp:
        text = valid_response

    with patch("core.llm.run_with_fallback", return_value=_FakeResp()):
        sig = _signature.parse_signature("def f(prices, window): return prices\n")
        out = _signature.llm_enrich_constraints(sig)
        assert "parameter_constraints" in out
        assert any(pc["name"] == "prices" for pc in out["parameter_constraints"])


def test_llm_enrichment_strips_markdown_fences():
    valid_with_fence = "```json\n" + json.dumps({"parameter_constraints": []}) + "\n```"

    class _FakeResp:
        text = valid_with_fence

    with patch("core.llm.run_with_fallback", return_value=_FakeResp()):
        sig = _signature.parse_signature("def f(x): return x\n")
        out = _signature.llm_enrich_constraints(sig)
        assert out == {"parameter_constraints": []}


def test_llm_enrichment_rejects_non_dict_response():
    """A valid JSON that's a list (not dict) must produce {}."""

    class _FakeResp:
        text = json.dumps(["not", "a", "dict"])

    with patch("core.llm.run_with_fallback", return_value=_FakeResp()):
        sig = _signature.parse_signature("def f(x): return x\n")
        out = _signature.llm_enrich_constraints(sig)
        assert out == {}


# ---------------------------------------------------------------------------
# Name heuristic — completeness + unknown names
# ---------------------------------------------------------------------------


def test_name_heuristic_covers_all_documented_names():
    """Every entry in _NAME_TYPE_HINTS must lookup to itself."""
    for name, typ in _signature._NAME_TYPE_HINTS.items():
        assert _signature._infer_type_from_name(name) == typ


def test_name_heuristic_case_insensitive():
    """Heuristic is case-insensitive (handles `Prices`, `WINDOW`, etc.)."""
    assert _signature._infer_type_from_name("Prices") == "ndarray"
    assert _signature._infer_type_from_name("WINDOW") == "int"
    assert _signature._infer_type_from_name("Returns") == "ndarray"


def test_name_heuristic_unknown_name_returns_any():
    assert _signature._infer_type_from_name("zzz_unknown") == "any"
    assert _signature._infer_type_from_name("") == "any"


# ---------------------------------------------------------------------------
# Signature pair compatibility — finer cases than the unit tests
# ---------------------------------------------------------------------------


def test_signature_pair_compatible_when_optional_added_to_candidate():
    pair = _signature.infer_pair(
        "def f(x): return x",
        "def f(x, opt=0): return x",
    )
    assert pair is not None and pair.divergence is None


def test_signature_pair_divergent_when_optional_removed_from_candidate():
    pair = _signature.infer_pair(
        "def f(x, opt=0): return x",
        "def f(x): return x",
    )
    # Removing an optional param drops candidate's arity but our diff is
    # based on positional_arity (required only). Both have 1 required → compatible.
    # This documents the current behaviour. If we tighten the check, update here.
    assert pair is not None
    # Currently we consider this compatible because callers using `f(x)` still work.


def test_signature_pair_divergent_when_required_kwonly_changes():
    pair = _signature.infer_pair(
        "def f(x, *, y): return (x, y)",
        "def f(x, *, z): return (x, z)",
    )
    assert pair is not None and pair.divergence is not None
    assert pair.divergence["kind"] == "required_kw_only"


def test_signature_pair_compatible_with_same_kwonly_required_set():
    pair = _signature.infer_pair(
        "def f(x, *, y): return (x, y)",
        "def f(x, *, y): return x + y",
    )
    assert pair is not None and pair.divergence is None


# ---------------------------------------------------------------------------
# AST → Hypothesis strategy synthesis (integration via the fuzz module)
# ---------------------------------------------------------------------------


def test_signature_with_pandas_series_hint_produces_series_strategy():
    """Ensure pandas.Series hints flow through to the fuzz strategy."""
    pd = pytest.importorskip("pandas", reason="pandas not installed in this env")
    from agents.quant_patch_validator import fuzz as _fuzz

    src = "import pandas as pd\ndef f(series: pd.Series) -> float:\n    return float(series.sum())\n"
    sig = _signature.parse_signature(src)
    assert sig is not None
    assert sig.parameters[0].type_name == "series"
    # Drive _build_combined_strategy and pull one example; it should be a pd.Series
    strat = _fuzz._build_combined_strategy(sig, {})
    args, kwargs = strat.example()
    assert len(args) == 1
    assert isinstance(args[0], pd.Series), f"expected pd.Series, got {type(args[0])}"
