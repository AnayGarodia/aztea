"""LLM error hierarchy used by all providers and the fallback chain.

All provider implementations must raise subclasses of ``LLMError`` — never
raw ``Exception`` or ``requests.HTTPError``. This allows ``run_with_fallback``
to catch and handle them uniformly.

Error types and fallback behaviour:
- ``LLMRateLimitError`` — provider is throttling; fallback chain skips to next
  provider. Carries an optional ``retry_after_seconds`` hint.
- ``LLMTimeoutError`` — the request timed out; fallback chain skips to next
  provider. Treat as transient.
- ``LLMAuthError`` — bad API key or quota exhausted; fallback chain skips but
  should alert (auth errors on a configured provider are unexpected).
- ``LLMBadResponseError`` — the provider returned an unparseable or
  structurally invalid response; fallback chain skips to next provider.
- ``LLMError`` (base) — any other provider-level failure; fallback chain skips.
"""
from __future__ import annotations


class LLMError(Exception):
    """Base class for all LLM provider errors."""

    def __init__(
        self,
        provider: str,
        model: str,
        message: str,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(f"[{provider}:{model}] {message}")
        self.provider = provider
        self.model = model
        self.cause = cause


class LLMRateLimitError(LLMError):
    """Provider is rate-limiting this key. Fallback chain moves to next provider."""

    def __init__(self, *args, retry_after_seconds: int | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Hint from the provider's Retry-After header, if present.
        self.retry_after_seconds = retry_after_seconds


class LLMTimeoutError(LLMError):
    """Request timed out waiting for the provider. Treated as transient."""


class LLMAuthError(LLMError):
    """API key invalid or quota exhausted. Usually requires operator action."""


class LLMBadResponseError(LLMError):
    """Provider returned a response that could not be parsed or is structurally invalid."""
