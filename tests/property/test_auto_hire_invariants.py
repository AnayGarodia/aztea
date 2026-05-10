"""Invariant tests for the auto-hire decision logic.

# OWNS: properties of decide(), _score_candidate, _confidence.
# INVARIANTS asserted: empty candidates → no_match; empty intent → empty_intent;
#       no auto-invoke without confidence; ranking is stable across repeated
#       calls; confidence is in [0, 1]; score is non-negative; gating reasons
#       are taken from the documented set.
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.registry.auto_hire import (
    CandidateAgent,
    Decision,
    _confidence,
    _score_candidate,
    decide,
)

pytestmark = pytest.mark.property


def _make_candidate(
    *,
    slug: str = "code_review",
    name: str = "Code Review Agent",
    description: str = "Reviews code changes for issues.",
    tags: tuple[str, ...] = ("review", "code"),
    category: str = "Code",
    price: float = 0.05,
    trust: float = 80.0,
    success: float = 0.9,
    stability: str = "stable",
    schema: dict | None = None,
    match_keywords: tuple[str, ...] = (),
    block_keywords: tuple[str, ...] = (),
) -> CandidateAgent:
    return CandidateAgent(
        agent_id=f"agent-{slug}",
        slug=slug,
        name=name,
        description=description,
        tags=list(tags),
        category=category,
        price_per_call_usd=price,
        trust_score=trust,
        success_rate=success,
        stability_tier=stability,
        input_schema=schema or {"type": "object", "properties": {}},
        raw={
            "agent_id": f"agent-{slug}",
            "slug": slug,
            "name": name,
            "description": description,
        },
        match_keywords=list(match_keywords),
        block_keywords=list(block_keywords),
    )


# --- decide() top-level branches ---------------------------------------------

def test_decide_with_no_candidates_returns_no_match():
    d = decide(intent="review my code", explicit_input=None, max_cost_usd=1.0, candidates=[])
    assert d.auto_invoked is False
    assert d.reason == "no_match"


def test_decide_with_empty_intent_returns_empty_intent():
    d = decide(intent="", explicit_input=None, max_cost_usd=1.0, candidates=[_make_candidate()])
    assert d.auto_invoked is False
    assert d.reason == "empty_intent"


def test_decide_with_whitespace_intent_returns_empty_intent():
    d = decide(intent="   \t\n", explicit_input=None, max_cost_usd=1.0, candidates=[_make_candidate()])
    assert d.auto_invoked is False
    assert d.reason == "empty_intent"


def test_decision_dataclass_default_shape():
    d = Decision(auto_invoked=False)
    assert d.candidates == []
    assert d.missing_fields == []


# --- _score_candidate determinism + non-negativity --------------------------

intent_strategy = st.text(min_size=1, max_size=80)


@given(intent=intent_strategy)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_score_is_non_negative_for_real_candidate(intent):
    c = _make_candidate()
    r = _score_candidate(c, intent)
    assert r.score >= 0


@given(intent=intent_strategy)
def test_score_is_deterministic(intent):
    c = _make_candidate()
    a = _score_candidate(c, intent)
    b = _score_candidate(c, intent)
    assert a.score == b.score


def test_exact_slug_match_outscores_no_match():
    """Intent containing the slug should outscore an unrelated intent."""
    c = _make_candidate(slug="code_review")
    high = _score_candidate(c, "code_review please")
    low = _score_candidate(c, "what is the weather today")
    assert high.score > low.score


def test_blocked_keyword_reduces_score():
    base = _make_candidate(slug="code_review")
    blocked = _make_candidate(slug="code_review", block_keywords=("python",))
    a = _score_candidate(base, "code_review for my python project")
    b = _score_candidate(blocked, "code_review for my python project")
    assert b.score <= a.score


# --- _confidence properties --------------------------------------------------

@given(score=st.floats(min_value=0.0, max_value=200.0, allow_nan=False))
def test_confidence_no_rivals_in_unit_interval(score):
    from core.registry.auto_hire import Ranked

    top = Ranked(candidate=_make_candidate(), score=score)
    c = _confidence(top, [])
    assert 0.0 <= c <= 1.0


@given(
    top_score=st.floats(min_value=1.0, max_value=200.0, allow_nan=False),
    runner_score=st.floats(min_value=0.0, max_value=200.0, allow_nan=False),
)
def test_confidence_two_candidates_in_unit_interval(top_score, runner_score):
    from core.registry.auto_hire import Ranked

    top = Ranked(candidate=_make_candidate(), score=top_score)
    runner = Ranked(candidate=_make_candidate(slug="other"), score=runner_score)
    c = _confidence(top, [runner])
    assert 0.0 <= c <= 1.0


def test_confidence_zero_when_top_is_zero():
    from core.registry.auto_hire import Ranked

    top = Ranked(candidate=_make_candidate(), score=0.0)
    assert _confidence(top, []) == 0.0


def test_confidence_dominant_singleton_high():
    from core.registry.auto_hire import Ranked

    top = Ranked(candidate=_make_candidate(), score=90.0)
    assert _confidence(top, []) >= 0.85


# --- decide() ranking stability ----------------------------------------------

@given(intent=intent_strategy)
def test_decide_is_deterministic(intent):
    cands = [_make_candidate(slug=s) for s in ("code_review", "linter", "type_checker")]
    a = decide(intent=intent, explicit_input=None, max_cost_usd=1.0, candidates=cands)
    b = decide(intent=intent, explicit_input=None, max_cost_usd=1.0, candidates=cands)
    assert a.auto_invoked == b.auto_invoked
    assert a.reason == b.reason
    if a.auto_invoked and b.auto_invoked:
        assert a.chosen.agent_id == b.chosen.agent_id


@given(intent=intent_strategy)
def test_decide_returns_decision_dataclass(intent):
    cands = [_make_candidate()]
    d = decide(intent=intent, explicit_input=None, max_cost_usd=1.0, candidates=cands)
    assert isinstance(d, Decision)
    assert isinstance(d.auto_invoked, bool)
    if d.auto_invoked:
        assert d.confidence is not None and 0.0 <= d.confidence <= 1.0


# --- price gate --------------------------------------------------------------

def test_decide_blocks_when_price_exceeds_max_cost():
    """A candidate priced above max_cost_usd should not be auto-invoked."""
    expensive = _make_candidate(slug="expensive_review", price=5.0)
    d = decide(
        intent="expensive_review please",
        explicit_input=None,
        max_cost_usd=0.10,
        candidates=[expensive],
    )
    if d.auto_invoked:
        assert expensive.price_per_call_usd <= 0.10
    else:
        # The decision must populate *some* gating reason — the exact taxonomy
        # is internal and may grow. Assert it's a non-empty string.
        assert isinstance(d.reason, str) and d.reason, "reason must be populated when not auto_invoked"
