"""Provider-agnostic LLM dispatch with automatic fallback across the chain.

The main entry point for all LLM calls in built-in agents and judges is
``run_with_fallback``. It iterates through the configured provider chain,
skipping providers that are unavailable or that return transient errors, and
raises only when every provider in the chain has been exhausted.

Usage pattern (correct):
    req = CompletionRequest(
        messages=[Message(role="user", content="hello")],
        temperature=0.2,
        max_tokens=512,
    )
    response = run_with_fallback(req)
    text = response.text   # always .text, never .content

Do NOT pass ``model=`` — the chain selects the model per provider.
"""

from __future__ import annotations

from dataclasses import replace

from .base import CompletionRequest, LLMResponse
from .errors import LLMError, LLMRateLimitError, LLMTimeoutError


def run_with_fallback(
    req_template: CompletionRequest,
    model_chain: list[str] | None = None,
    *,
    caller_api_key_id: str | None = None,
) -> LLMResponse:
    """Dispatch a completion request through the provider chain.

    Tries each provider in ``model_chain`` (or ``DEFAULT_CHAIN`` if None) in
    order. Skips a provider if:
    - it is not configured (``is_available()`` returns False), or
    - it raises ``LLMRateLimitError`` or ``LLMTimeoutError`` (transient).

    Any other ``LLMError`` also causes the chain to advance, so a single
    bad-key or bad-response error on one provider does not block the rest.

    When ``caller_api_key_id`` is provided AND an
    ``AZTEA_BYOK_<id>_<provider>_API_KEY`` env override exists, the
    per-caller provider is used in place of the platform default for that
    spec (audit 2026-05-17 bug #5). Without an override, the platform
    default is used and a once-per-process warning is logged so operators
    can spot the shared-quota gap.

    Raises the last encountered ``LLMError`` if every provider fails, or a
    plain ``LLMError`` with provider="none" if the chain is empty / all
    providers are unavailable.
    """
    from .registry import DEFAULT_CHAIN, resolve, resolve_for_caller

    chain = model_chain if model_chain is not None else DEFAULT_CHAIN
    last_error: LLMError | None = None

    for spec in chain:
        try:
            if caller_api_key_id:
                provider, model = resolve_for_caller(
                    spec, caller_api_key_id=caller_api_key_id,
                )
            else:
                provider, model = resolve(spec)
        except ValueError:
            continue

        if not provider.is_available():
            continue

        req = replace(req_template, model=model)
        try:
            return provider.complete(req)
        except (LLMRateLimitError, LLMTimeoutError) as exc:
            last_error = exc
            continue
        except LLMError as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise LLMError("none", "", "No available LLM providers in chain: " + str(chain))
