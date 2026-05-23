"""Tests for the LLM budget cap and pricing helpers."""

from __future__ import annotations

import pytest

from core.llm.base import CompletionRequest, LLMResponse, Message, Usage
from core.llm.errors import BudgetExceededError, LLMError
from core.llm.fallback import run_with_fallback
from core.llm.pricing import (
    ProviderRate,
    estimate_cost,
    estimate_request_cost,
)


# ---------------------------------------------------------------------------
# pricing helpers
# ---------------------------------------------------------------------------


def test_estimate_cost_known_provider_uses_seed_rate():
    # openai seed: 1c/1k prompt, 2c/1k completion
    cost = estimate_cost("openai", "gpt-4o", prompt_tokens=10_000, completion_tokens=5_000)
    assert cost == 10 + 10


def test_estimate_cost_unknown_provider_uses_conservative_default():
    # default: 1c/1k prompt, 5c/1k completion
    cost = estimate_cost("never_heard_of_it", "x", prompt_tokens=1000, completion_tokens=1000)
    assert cost == 1 + 5


def test_estimate_cost_rounds_up_to_next_cent():
    # 50 tokens at 1c/1k = 0.05c → rounds up to 1c
    cost = estimate_cost("openai", "gpt-4o", prompt_tokens=50, completion_tokens=0)
    assert cost == 1


def test_estimate_cost_zero_tokens_returns_zero():
    assert estimate_cost("openai", "gpt-4o", 0, 0) == 0


def test_estimate_cost_negative_tokens_raises():
    with pytest.raises(ValueError, match="non-negative"):
        estimate_cost("openai", "gpt-4o", -1, 0)


def test_estimate_request_cost_uses_char_heuristic():
    # 4000 chars ≈ 1000 prompt tokens at 4 chars/token
    cost = estimate_request_cost("openai", "gpt-4o",
                                  prompt_char_count=4000,
                                  max_completion_tokens=1000)
    # 1000 prompt × 1c/1k + 1000 completion × 2c/1k = 1 + 2 = 3
    assert cost == 3


def test_env_override_changes_rate(monkeypatch):
    monkeypatch.setenv("AZTEA_LLM_PRICE_OPENAI_DEFAULT_PROMPT_CENTS_PER_1K", "10")
    cost = estimate_cost("openai", "", prompt_tokens=1000, completion_tokens=0)
    assert cost == 10


def test_env_override_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("AZTEA_LLM_PRICE_OPENAI_DEFAULT_PROMPT_CENTS_PER_1K", "not-a-number")
    cost = estimate_cost("openai", "", prompt_tokens=1000, completion_tokens=0)
    # falls back to seed rate
    assert cost == 1


# ---------------------------------------------------------------------------
# run_with_fallback budget gate
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Mock provider for fallback tests. Counts how many times complete() is called."""

    def __init__(self, name: str, model: str, available: bool = True,
                 raise_error: bool = False,
                 usage: Usage | None = None):
        self.name = name
        self._model = model
        self._available = available
        self._raise = raise_error
        self._usage = usage or Usage(prompt_tokens=100, completion_tokens=50)
        self.complete_calls: int = 0

    def is_available(self) -> bool:
        return self._available

    def complete(self, req):
        self.complete_calls += 1
        if self._raise:
            raise LLMError(self.name, self._model, "forced failure")
        return LLMResponse(
            text="ok",
            model=req.model,
            provider=self.name,
            usage=self._usage,
        )


def _patch_chain(monkeypatch, providers: list[_FakeProvider]):
    """Replace the registry resolution so run_with_fallback uses our fakes."""
    from core.llm import fallback as fb_mod

    chain_specs = [f"spec_{i}" for i in range(len(providers))]

    def fake_resolve(spec):
        idx = int(spec.split("_")[1])
        p = providers[idx]
        return p, p._model

    def fake_resolve_for_caller(spec, caller_api_key_id):
        return fake_resolve(spec)

    # Monkeypatch the lazy-imported registry module
    import core.llm.registry as reg
    monkeypatch.setattr(reg, "resolve", fake_resolve)
    monkeypatch.setattr(reg, "resolve_for_caller", fake_resolve_for_caller)
    monkeypatch.setattr(reg, "DEFAULT_CHAIN", chain_specs)
    return chain_specs


def _basic_request(max_tokens: int = 100) -> CompletionRequest:
    return CompletionRequest(
        model="",
        messages=[Message(role="user", content="hello world")],
        temperature=0.0,
        max_tokens=max_tokens,
    )


def test_budget_zero_disables_cap(monkeypatch):
    p = _FakeProvider("openai", "gpt-4o")
    _patch_chain(monkeypatch, [p])
    response = run_with_fallback(_basic_request(max_tokens=10_000), budget_cents=0)
    assert response.text == "ok"
    assert p.complete_calls == 1


def test_budget_one_cent_with_huge_prompt_raises_before_call(monkeypatch):
    p = _FakeProvider("openai", "gpt-4o")
    _patch_chain(monkeypatch, [p])
    huge_request = CompletionRequest(
        model="",
        messages=[Message(role="user", content="x" * 100_000)],
        max_tokens=10_000,
    )
    with pytest.raises(BudgetExceededError) as exc_info:
        run_with_fallback(huge_request, budget_cents=1)
    assert exc_info.value.budget_cents == 1
    assert exc_info.value.spent_cents == 0
    assert exc_info.value.estimated_next_cents > 1
    assert p.complete_calls == 0


def test_budget_callback_fires_with_cumulative_usage(monkeypatch):
    p = _FakeProvider("openai", "gpt-4o",
                       usage=Usage(prompt_tokens=200, completion_tokens=100))
    _patch_chain(monkeypatch, [p])
    seen: list[Usage] = []
    response = run_with_fallback(
        _basic_request(),
        budget_cents=1000,
        usage_callback=seen.append,
    )
    assert response.text == "ok"
    assert len(seen) == 1
    assert seen[0].prompt_tokens == 200
    assert seen[0].completion_tokens == 100


def test_default_budget_constant_is_present():
    from core.llm.fallback import _DEFAULT_LLM_BUDGET_CENTS
    assert _DEFAULT_LLM_BUDGET_CENTS == 50


def test_budget_advances_through_chain_on_provider_error(monkeypatch):
    bad = _FakeProvider("groq", "llama-3", raise_error=True)
    good = _FakeProvider("openai", "gpt-4o")
    _patch_chain(monkeypatch, [bad, good])
    response = run_with_fallback(_basic_request(), budget_cents=1000)
    assert response.provider == "openai"
    assert bad.complete_calls == 1
    assert good.complete_calls == 1


def test_provider_rate_dataclass_is_frozen():
    rate = ProviderRate(prompt_cents_per_1k=1, completion_cents_per_1k=2)
    with pytest.raises((AttributeError, TypeError)):
        rate.prompt_cents_per_1k = 99


def test_budget_exceeded_error_carries_diagnostic_fields():
    err = BudgetExceededError(
        "openai", "gpt-4o", "test",
        budget_cents=10, spent_cents=8, estimated_next_cents=5,
    )
    assert err.budget_cents == 10
    assert err.spent_cents == 8
    assert err.estimated_next_cents == 5
    assert err.provider == "openai"
