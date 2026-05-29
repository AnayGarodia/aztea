# SPDX-License-Identifier: Apache-2.0
"""Tests for the auto_hire decision cache (Phase 3).

Covers:
  - cache hit on identical intent within TTL
  - cache miss after catalog_broadcast.bump() invalidates
  - bypass_cache=True forces fresh compute
  - confidence near the gating threshold is NOT cached (borderline skip)
"""
from __future__ import annotations

import pytest

from core.registry import auto_hire, catalog_broadcast


@pytest.fixture(autouse=True)
def _reset_decision_cache() -> None:
    """Each test gets a clean cache."""
    auto_hire._invalidate_decision_cache()
    yield
    auto_hire._invalidate_decision_cache()


def _make_candidate(
    slug: str = "probe",
    *,
    match_keywords: list[str] | None = None,
    price_usd: float = 0.01,
    trust: float = 80.0,
    success: float = 0.95,
) -> auto_hire.CandidateAgent:
    """Build a minimal CandidateAgent for cache tests."""
    return auto_hire.CandidateAgent(
        agent_id=f"agent-{slug}",
        slug=slug,
        name=slug.replace("_", " ").title(),
        description=f"A test agent named {slug}.",
        tags=[slug],
        category="testing",
        price_per_call_usd=price_usd,
        trust_score=trust,
        success_rate=success,
        stability_tier="stable",
        input_schema={
            "type": "object",
            "properties": {"intent": {"type": "string"}},
            "required": ["intent"],
        },
        match_keywords=match_keywords or [slug],
        block_keywords=[],
        raw={
            "agent_id": f"agent-{slug}",
            "slug": slug,
            "name": slug.replace("_", " ").title(),
            "review_status": "approved",
            "total_calls": 100,
            "successful_calls": 95,
        },
    )


def test_cache_hit_on_identical_intent() -> None:
    """Same intent within TTL returns the cached decision with cached=True."""
    cands = [_make_candidate("probe", match_keywords=["probe", "test"])]
    intent = "probe this test thing"

    first, meta_first = auto_hire.decide_cached(
        intent=intent,
        explicit_input={"intent": intent},
        max_cost_usd=1.0,
        candidates=cands,
    )
    assert meta_first["cached"] is False  # fresh compute

    second, meta_second = auto_hire.decide_cached(
        intent=intent,
        explicit_input={"intent": intent},
        max_cost_usd=1.0,
        candidates=cands,
    )
    # Cached decision is returned with cached=True. The Decision objects are
    # the same instance (cache returns references), but we assert on the
    # observable meta field rather than identity for clarity.
    assert meta_second["cached"] is True
    assert meta_second["cached_at"] is not None
    # Decision content should match.
    assert first.auto_invoked == second.auto_invoked
    assert first.reason == second.reason


def test_catalog_bump_invalidates_cache() -> None:
    """A catalog mutation (bump) invalidates all cached decisions."""
    cands = [_make_candidate("probe", match_keywords=["probe", "audit"])]
    intent = "audit this probe"

    _, meta_first = auto_hire.decide_cached(
        intent=intent,
        explicit_input={"intent": intent},
        max_cost_usd=1.0,
        candidates=cands,
    )
    assert meta_first["cached"] is False

    # Simulate a catalog mutation.
    catalog_broadcast.bump()

    _, meta_after_bump = auto_hire.decide_cached(
        intent=intent,
        explicit_input={"intent": intent},
        max_cost_usd=1.0,
        candidates=cands,
    )
    # Cache invalidated → fresh compute.
    assert meta_after_bump["cached"] is False


def test_bypass_cache_forces_fresh_compute() -> None:
    """bypass_cache=True ignores any cached entry."""
    cands = [_make_candidate("probe", match_keywords=["probe"])]
    intent = "probe stuff"

    auto_hire.decide_cached(
        intent=intent,
        explicit_input={"intent": intent},
        max_cost_usd=1.0,
        candidates=cands,
    )
    # Now read with bypass=True — should NOT report cached.
    _, meta = auto_hire.decide_cached(
        intent=intent,
        explicit_input={"intent": intent},
        max_cost_usd=1.0,
        candidates=cands,
        bypass_cache=True,
    )
    assert meta["cached"] is False


def test_different_intent_misses_cache() -> None:
    """Different intent_hash → cache miss."""
    cands = [_make_candidate("probe", match_keywords=["probe", "audit"])]

    auto_hire.decide_cached(
        intent="audit one",
        explicit_input={"intent": "audit one"},
        max_cost_usd=1.0,
        candidates=cands,
    )
    _, meta_b = auto_hire.decide_cached(
        intent="audit two",
        explicit_input={"intent": "audit two"},
        max_cost_usd=1.0,
        candidates=cands,
    )
    assert meta_b["cached"] is False


def test_borderline_confidence_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decisions whose confidence is within ±0.05 of the gating threshold
    must not be cached — they are too sensitive to env-driven threshold
    tweaks during the TTL window. _is_borderline is the gate.
    """
    # Use the default threshold of 0.30 (no env override).
    monkeypatch.delenv("AZTEA_AUTO_INVOKE_CONFIDENCE", raising=False)

    # Near-threshold (within ±0.05): NOT cacheable.
    near_low = auto_hire.Decision(auto_invoked=True, confidence=0.27)
    near_high = auto_hire.Decision(auto_invoked=True, confidence=0.33)
    assert auto_hire._is_borderline(near_low, aggressive=False) is True
    assert auto_hire._is_borderline(near_high, aggressive=False) is True

    # Far from threshold: cacheable.
    far_above = auto_hire.Decision(auto_invoked=True, confidence=0.80)
    far_below = auto_hire.Decision(auto_invoked=False, confidence=0.05)
    assert auto_hire._is_borderline(far_above, aggressive=False) is False
    assert auto_hire._is_borderline(far_below, aggressive=False) is False

    # Aggressive mode shifts the threshold to 0.20.
    aggressive_near = auto_hire.Decision(auto_invoked=True, confidence=0.22)
    assert auto_hire._is_borderline(aggressive_near, aggressive=True) is True
    assert auto_hire._is_borderline(aggressive_near, aggressive=False) is False


def test_none_confidence_is_not_borderline() -> None:
    """Decisions without a confidence score (e.g. no_match) are not borderline."""
    no_conf = auto_hire.Decision(auto_invoked=False, reason="no_match", confidence=None)
    assert auto_hire._is_borderline(no_conf, aggressive=False) is False
