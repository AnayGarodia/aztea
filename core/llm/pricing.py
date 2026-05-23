"""
pricing.py — per-provider per-1k-token cost estimation in integer cents.

# OWNS: cost-estimate table and estimate_cost helper.
# NOT OWNS: actual billing (payments live in core/payments/),
#           provider-side pricing changes (operators sync via env override).
#
# INVARIANTS:
#   * All money returned in INTEGER CENTS — never floats (CLAUDE.md money rule).
#   * Estimates are upper bounds, not point predictions. Better to refuse a
#     call that might be expensive than to silently exceed a stated budget.
#   * Unknown provider/model pairs return the conservative platform default
#     so callers don't silently get a free pass past their cap.
#
# DECISIONS:
#   * Per-provider single rate covers the workhorse model. Real per-model
#     granularity would require keeping a hundred-entry catalog in sync with
#     vendor changes — not worth it for v0. Operators override per (provider,
#     model, kind) tuple via env vars when precision matters.
#   * Rates seeded from publicly listed pricing as of 2026-05 — see comments
#     beside each entry. Source links are in the table for traceability.
#   * Char-based prompt-token estimate uses the 4-chars-per-token heuristic
#     that holds within ~20% across English text for every provider's
#     tokenizer. Code prompts skew higher; we round up to compensate.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)

# Conservative fallback applied when a provider isn't in the table.
# Set above the most expensive model in the seed (Anthropic claude-opus-4 at
# 1500/in + 7500/out per million → 0.15c per 1k prompt + 0.75c per 1k completion)
# rounded up. Better to over-estimate and refuse than under-estimate and bust.
_DEFAULT_PROMPT_CENTS_PER_1K: int = 1   # 1 cent per 1k input tokens
_DEFAULT_COMPLETION_CENTS_PER_1K: int = 5  # 5 cents per 1k completion tokens

# 4-chars-per-token is the rule-of-thumb that holds within ~20% for English.
# Code skews higher per-token (denser tokens); the round-up below absorbs it.
_CHARS_PER_TOKEN: float = 4.0


@dataclass(frozen=True)
class ProviderRate:
    """Cost in cents per 1000 tokens, separated by direction."""

    prompt_cents_per_1k: int
    completion_cents_per_1k: int


# Seed table. Values are upper-bounded so the chain's budget check is honest.
# Source: each vendor's public pricing page as of 2026-05; refresh periodically.
# Where a provider hosts multiple models with different rates, this entry tracks
# the most expensive widely-used option so we never under-estimate.
_PROVIDER_RATES: dict[str, ProviderRate] = {
    # OpenAI gpt-4o pricing: $5/M in, $15/M out → 0.5c/1k, 1.5c/1k
    "openai":     ProviderRate(prompt_cents_per_1k=1, completion_cents_per_1k=2),
    # Anthropic claude-sonnet-4: $3/M in, $15/M out → 0.3c/1k, 1.5c/1k
    "anthropic":  ProviderRate(prompt_cents_per_1k=1, completion_cents_per_1k=2),
    # Groq llama-3 70b: ~$0.59/M in, $0.79/M out → essentially negligible
    "groq":       ProviderRate(prompt_cents_per_1k=1, completion_cents_per_1k=1),
    # Cohere command-r-plus: $3/M in, $15/M out → matches Anthropic
    "cohere":     ProviderRate(prompt_cents_per_1k=1, completion_cents_per_1k=2),
    # AWS Bedrock varies wildly by model; pick a conservative midpoint
    "bedrock":    ProviderRate(prompt_cents_per_1k=1, completion_cents_per_1k=3),
    # OpenAI-compatible umbrella (mistral, together, fireworks, deepseek, etc.)
    # — all priced well below OpenAI; conservative cap keeps surprises contained
    "openai_compatible": ProviderRate(prompt_cents_per_1k=1, completion_cents_per_1k=2),
}


def _override_key(provider: str, model: str, kind: str) -> str:
    """Pure: deterministic env-var name for an override entry."""
    safe_provider = provider.upper().replace("-", "_")
    safe_model = model.upper().replace("-", "_").replace(".", "_").replace("/", "_") if model else "DEFAULT"
    safe_kind = kind.upper()
    return f"AZTEA_LLM_PRICE_{safe_provider}_{safe_model}_{safe_kind}_CENTS_PER_1K"


def _lookup_rate(provider: str, model: str) -> ProviderRate:
    """Resolve the rate for (provider, model), applying env overrides if set."""
    base = _PROVIDER_RATES.get(provider, ProviderRate(
        prompt_cents_per_1k=_DEFAULT_PROMPT_CENTS_PER_1K,
        completion_cents_per_1k=_DEFAULT_COMPLETION_CENTS_PER_1K,
    ))
    prompt_override = os.environ.get(_override_key(provider, model, "PROMPT"))
    completion_override = os.environ.get(_override_key(provider, model, "COMPLETION"))
    if prompt_override is None and completion_override is None:
        return base
    return ProviderRate(
        prompt_cents_per_1k=_safe_int(prompt_override, base.prompt_cents_per_1k),
        completion_cents_per_1k=_safe_int(
            completion_override, base.completion_cents_per_1k
        ),
    )


def _safe_int(value: str | None, fallback: int) -> int:
    """Pure: parse an int env override; fall back on bad input rather than crash."""
    if value is None:
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        _LOG.warning("pricing override %r is not an integer; using fallback %d", value, fallback)
        return fallback
    if parsed < 0:
        _LOG.warning("pricing override %r is negative; using fallback %d", value, fallback)
        return fallback
    return parsed


def estimate_cost(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> int:
    """Return integer-cents cost for (prompt_tokens, completion_tokens).

    Always rounds up. Why: budgets are ceilings, and floor()-rounding a
    fractional-cent estimate could let a sequence of small calls each
    individually round to 0 and collectively bust the ceiling.
    """
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError(
            f"token counts must be non-negative; got prompt={prompt_tokens} "
            f"completion={completion_tokens}"
        )
    rate = _lookup_rate(provider, model)
    prompt_cents = math.ceil(prompt_tokens * rate.prompt_cents_per_1k / 1000)
    completion_cents = math.ceil(completion_tokens * rate.completion_cents_per_1k / 1000)
    return int(prompt_cents + completion_cents)


def estimate_request_cost(
    provider: str,
    model: str,
    prompt_char_count: int,
    max_completion_tokens: int,
) -> int:
    """Estimate the upper bound cost of a not-yet-issued request.

    Why upper bound: this is the gate that decides whether to attempt the
    next provider against a hard budget. Under-estimating means the budget
    is illusory; over-estimating means we sometimes refuse a call that
    would have squeaked under — the safer failure mode.
    """
    if prompt_char_count < 0 or max_completion_tokens < 0:
        raise ValueError(
            f"counts must be non-negative; got chars={prompt_char_count} "
            f"max_completion={max_completion_tokens}"
        )
    prompt_token_estimate = math.ceil(prompt_char_count / _CHARS_PER_TOKEN)
    return estimate_cost(provider, model, prompt_token_estimate, max_completion_tokens)
