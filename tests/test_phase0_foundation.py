"""Phase 0 (2026-05-28): foundation tests.

Covers:
- Refusal-reason taxonomy lock-down (core/error_codes.py)
- Feature flag dependency enforcement (core/feature_flags.py)
- render_refusal across output_format values (core/output_formats.py)
- Reflex eval harness fixture loader (tests/eval/reflex/runner.py)
"""

from __future__ import annotations

import json

import pytest

from core import error_codes as ec
from core import feature_flags as ff
from core import output_formats as of


# --- Refusal taxonomy --------------------------------------------------


def test_auto_hire_reasons_locked_in_taxonomy():
    """Every reason code emitted by auto_hire.py MUST be in the locked set."""
    expected = {
        ec.AUTO_HIRE_NO_MATCH,
        ec.AUTO_HIRE_LOW_CONFIDENCE,
        ec.AUTO_HIRE_LOW_TRUST,
        ec.AUTO_HIRE_LOW_SUCCESS_RATE,
        ec.AUTO_HIRE_BROKEN_AGENT,
        ec.AUTO_HIRE_BETA_AGENT,
        ec.AUTO_HIRE_PRICE_EXCEEDS_MAX,
        ec.AUTO_HIRE_MISSING_FIELDS,
        ec.AUTO_HIRE_DISABLED,
        ec.AUTO_HIRE_EMPTY_INTENT,
        ec.AUTO_HIRE_COMPOUND_INTENT,
        ec.AUTO_HIRE_TIEBREAKER_FAILED,
        ec.AUTO_HIRE_AGENT_RECENTLY_FLIPPED_BROKEN,
    }
    assert expected == ec.AUTO_HIRE_REASONS


def test_auto_hire_reasons_namespaced():
    """All codes use the `auto_hire.` prefix so callers can switch on them."""
    for code in ec.AUTO_HIRE_REASONS:
        assert code.startswith("auto_hire."), code


# --- Feature flag dependencies -----------------------------------------


def test_flag_dependencies_empty_when_all_off(monkeypatch):
    for env in (
        "AZTEA_AUTO_INVOKE_USE_LEARNED_RANKER",
        "AZTEA_AUTO_INVOKE_USE_CALIBRATED_CONFIDENCE",
        "AZTEA_AUTO_INVOKE_USE_EXAMPLE_INTENTS",
        "AZTEA_AUTO_INVOKE_USE_INTENT_CLASSIFIER",
    ):
        monkeypatch.delenv(env, raising=False)
    assert ff.check_auto_invoke_flag_dependencies() == []


def test_flag_dependencies_warn_on_unmet_learned_ranker(monkeypatch):
    monkeypatch.setenv("AZTEA_AUTO_INVOKE_USE_LEARNED_RANKER", "1")
    monkeypatch.delenv("AZTEA_AUTO_INVOKE_USE_CALIBRATED_CONFIDENCE", raising=False)
    warnings = ff.check_auto_invoke_flag_dependencies()
    assert any("USE_LEARNED_RANKER" in w for w in warnings)
    assert any("CALIBRATED_CONFIDENCE" in w for w in warnings)


def test_flag_dependencies_satisfied_when_both_set(monkeypatch):
    monkeypatch.setenv("AZTEA_AUTO_INVOKE_USE_LEARNED_RANKER", "1")
    monkeypatch.setenv("AZTEA_AUTO_INVOKE_USE_CALIBRATED_CONFIDENCE", "1")
    monkeypatch.delenv("AZTEA_AUTO_INVOKE_USE_EXAMPLE_INTENTS", raising=False)
    warnings = ff.check_auto_invoke_flag_dependencies()
    assert warnings == []


def test_flag_dependencies_examples_require_classifier(monkeypatch):
    monkeypatch.setenv("AZTEA_AUTO_INVOKE_USE_EXAMPLE_INTENTS", "1")
    monkeypatch.delenv("AZTEA_AUTO_INVOKE_USE_INTENT_CLASSIFIER", raising=False)
    warnings = ff.check_auto_invoke_flag_dependencies()
    assert any("USE_EXAMPLE_INTENTS" in w for w in warnings)


# --- render_refusal ----------------------------------------------------


@pytest.mark.parametrize("fmt", [
    "markdown", "github_pr_comment", "slack_blocks", "text",
])
def test_render_refusal_returns_string_for_known_formats(fmt):
    out = of.render_refusal(
        reason="low_confidence",
        next_step="Multiple agents could fit.",
        output_format=fmt,
        candidates=[{"slug": "agent_a"}, {"slug": "agent_b"}],
    )
    assert isinstance(out, str)
    assert len(out) > 0


def test_render_refusal_returns_none_for_unknown_format():
    assert of.render_refusal(
        reason="low_confidence",
        next_step="x",
        output_format="json",  # not in the refusal-render set
    ) is None


def test_render_refusal_returns_none_for_empty_format():
    assert of.render_refusal("x", "y", "") is None


def test_render_refusal_handles_missing_optionals():
    """No reason / next_step / candidates — must not crash, must return string."""
    out = of.render_refusal(None, None, "text")
    assert isinstance(out, str)
    assert "unspecified" in out


def test_render_refusal_slack_blocks_valid_json():
    out = of.render_refusal(
        reason="no_match", next_step="Try a broader query.",
        output_format="slack_blocks",
    )
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert parsed and parsed[0]["type"] == "section"


@pytest.mark.parametrize("reason,fmt", [
    (r, f) for r in [
        "no_match", "low_confidence", "low_trust", "broken_agent",
        "compound_intent", "missing_fields", "tiebreaker_failed",
    ] for f in ["markdown", "text", "github_pr_comment", "slack_blocks"]
])
def test_render_refusal_matrix(reason, fmt):
    """/autoplan D-5: every reason × every format renders without raising."""
    out = of.render_refusal(reason=reason, next_step="see docs", output_format=fmt)
    assert isinstance(out, str)


# --- Reflex eval harness -----------------------------------------------


def test_reflex_fixtures_load_and_validate():
    """Every fixture must conform to the schema. CI gate."""
    from tests.eval.reflex.runner import load_fixtures
    fixtures = load_fixtures()
    # At least one fixture ships with Phase 0.
    assert len(fixtures) >= 1
    for f in fixtures:
        assert f.id
        assert f.intent
        assert f.expected_specialist_slug
        assert f.failure_bucket_if_wrong in {
            "agent_used_native_tool",
            "agent_synthesized_from_training",
            "agent_called_wrong_specialist",
            "aztea_refused",
        }


def test_reflex_runner_main_returns_zero():
    """`python tests/eval/reflex/runner.py` exit code = 0."""
    from tests.eval.reflex.runner import main
    assert main([]) == 0
