"""Per-process LLM token bucket tests (/review M3+M4).

Distinct from tests/test_llm_budget.py — that file tests the
core/llm/fallback.py budget_cents knob for cost-capped completions.
This one tests core/registry/_llm_budget.py, a per-category token
bucket that caps routing-side LLM calls (tiebreaker, classifier, etc.).
"""

from __future__ import annotations

import pytest

from core.registry import _llm_budget as lb


@pytest.fixture(autouse=True)
def reset_buckets():
    # Reset before AND after so prior-test state doesn't leak in.
    lb.reset()
    yield
    lb.reset()


def test_known_categories_succeed_on_first_call():
    for cat in ("tiebreaker", "classifier", "whole_payload", "examples"):
        assert lb.try_consume(cat) is True


def test_consume_decrements_tokens():
    lb.try_consume("tiebreaker")
    snap1 = lb.status()
    lb.try_consume("tiebreaker")
    snap2 = lb.status()
    assert snap2["tiebreaker"]["tokens"] < snap1["tiebreaker"]["tokens"]


def test_consume_returns_false_when_exhausted(monkeypatch):
    # Inject a tiny-capacity category by patching _DEFAULT_CAPACITY.
    monkeypatch.setitem(lb._DEFAULT_CAPACITY, "test_cat", 3)
    lb.reset()
    assert lb.try_consume("test_cat") is True
    assert lb.try_consume("test_cat") is True
    assert lb.try_consume("test_cat") is True
    # Bucket should be empty; refill is over a 60s window, so 4th
    # immediate call must fail.
    assert lb.try_consume("test_cat") is False


def test_categories_are_independent(monkeypatch):
    """Burning classifier budget shouldn't touch the tiebreaker budget."""
    # Drain classifier completely.
    for _ in range(lb._DEFAULT_CAPACITY["classifier"] + 5):
        lb.try_consume("classifier")
    # Tiebreaker is untouched.
    assert lb.try_consume("tiebreaker") is True


def test_reset_refills_all_buckets():
    for _ in range(5):
        lb.try_consume("tiebreaker")
    lb.reset()
    snap = lb.status()
    if "tiebreaker" in snap:
        assert snap["tiebreaker"]["tokens"] == snap["tiebreaker"]["capacity"]


def test_status_reports_known_categories():
    lb.try_consume("classifier")
    snap = lb.status()
    assert "classifier" in snap
    assert "capacity" in snap["classifier"]
    assert "tokens" in snap["classifier"]
    assert "fraction" in snap["classifier"]
    assert 0.0 <= snap["classifier"]["fraction"] <= 1.0


# --- Belt-and-suspenders H1 layer 1: per-request RequestBudget ---


def test_request_budget_caps_total_calls():
    rb = lb.RequestBudget(cap=3)
    assert rb.try_consume("tiebreaker") is True
    assert rb.try_consume("classifier") is True
    assert rb.try_consume("whole_payload") is True
    # 4th call must fail regardless of category.
    assert rb.try_consume("tiebreaker") is False
    assert rb.try_consume("examples") is False


def test_request_budget_blocks_consume_even_when_global_has_headroom():
    """A burst within one orchestration is bounded regardless of bucket state."""
    rb = lb.RequestBudget(cap=1)
    # First consume succeeds — global has tons of headroom.
    assert lb.try_consume(
        "tiebreaker", caller_owner_id="caller-x", request_budget=rb,
    ) is True
    # Second consume fails at the per-request layer, NOT the global.
    assert lb.try_consume(
        "tiebreaker", caller_owner_id="caller-x", request_budget=rb,
    ) is False


def test_request_budget_failure_does_not_consume_global_or_per_caller():
    """If the per-request cap blocks, neither bucket should decrement."""
    rb = lb.RequestBudget(cap=0)  # immediately exhausted
    before = lb.status()["tiebreaker"]["tokens"]
    assert lb.try_consume(
        "tiebreaker", caller_owner_id="some-caller", request_budget=rb,
    ) is False
    after = lb.status()["tiebreaker"]["tokens"]
    # No global decrement since per-request layer refused first.
    assert before == after


def test_per_caller_failure_refunds_per_request_budget(monkeypatch):
    """Belt-and-suspenders: when the per-caller cap rejects, the
    per-request counter must be rolled back so subsequent legitimate
    calls don't double-count."""
    monkeypatch.setitem(lb._DEFAULT_CAPACITY, "tight_cat", 6)
    lb.reset()
    rb = lb.RequestBudget(cap=4)
    # Drain per-caller bucket for caller-y (per-caller is fraction of 6).
    per_caller_cap = lb._per_caller_capacity("tight_cat")
    for _ in range(per_caller_cap):
        lb.try_consume("tight_cat", caller_owner_id="caller-y")
    # Next call must fail at per-caller layer.
    used_before = rb.used
    assert lb.try_consume(
        "tight_cat", caller_owner_id="caller-y", request_budget=rb,
    ) is False
    # Per-request counter must NOT have been consumed (refunded).
    assert rb.used == used_before
