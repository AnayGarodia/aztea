from __future__ import annotations

from dataclasses import replace

from .base import CompletionRequest, LLMResponse
from .errors import LLMError, LLMRateLimitError, LLMTimeoutError


def run_with_fallback(
    req_template: CompletionRequest,
    model_chain: list[str] | None = None,
) -> LLMResponse:
    from .registry import DEFAULT_CHAIN, resolve

    chain = model_chain if model_chain is not None else DEFAULT_CHAIN
    last_error: LLMError | None = None

    for spec in chain:
        try:
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
