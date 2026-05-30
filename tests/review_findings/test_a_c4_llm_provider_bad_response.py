"""Finding A-C4 (2026-05-30 review): native LLM providers let raw exceptions
escape `run_with_fallback`.

`core/llm/fallback.py` only catches `LLMRateLimitError`, `LLMTimeoutError`, and
`LLMError`. Before the fix, the openai/groq/anthropic providers parsed the
response (`completion.choices[0]...`, `response.content[0].text`) OUTSIDE any
try/except, so a malformed response (empty `choices`/`content`) raised a raw
`IndexError`/`AttributeError` that escaped the provider — aborting the whole
fallback chain instead of failing over to the next provider.

After the fix, each provider wraps response parsing and re-raises
`LLMBadResponseError` (an `LLMError`), which the fallback layer catches.

These tests assert the post-fix contract: a malformed response → `LLMBadResponseError`.
"""

from __future__ import annotations

import unittest.mock as mock

import pytest

from core.llm.base import CompletionRequest, Message
from core.llm.errors import LLMBadResponseError, LLMError


def _req() -> CompletionRequest:
    return CompletionRequest(model="test-model", messages=[Message("user", "hi")])


def test_openai_empty_choices_raises_llm_error_not_indexerror():
    from core.llm.providers.openai_provider import OpenAIProvider

    p = OpenAIProvider.__new__(OpenAIProvider)
    p._available = True
    p._openai_mod = mock.MagicMock()

    bad = mock.MagicMock()
    bad.choices = []  # empty → choices[0] would IndexError
    client = mock.MagicMock()
    client.chat.completions.create.return_value = bad
    p._client = client

    with pytest.raises(LLMBadResponseError):
        p.complete(_req())


def test_groq_empty_choices_raises_llm_error_not_indexerror():
    from core.llm.providers.groq_provider import GroqProvider

    p = GroqProvider.__new__(GroqProvider)
    p._available = True
    p._groq_mod = mock.MagicMock()

    bad = mock.MagicMock()
    bad.choices = []
    client = mock.MagicMock()
    client.chat.completions.create.return_value = bad
    p._client = client

    with pytest.raises(LLMBadResponseError):
        p.complete(_req())


def test_anthropic_malformed_content_raises_llm_error():
    from core.llm.providers.anthropic_provider import AnthropicProvider

    p = AnthropicProvider.__new__(AnthropicProvider)
    p._available = True
    p._anthropic_mod = mock.MagicMock()

    bad = mock.MagicMock()
    # content present but element has no .text attribute that returns a str cleanly
    bad.content = [object()]  # accessing .text → AttributeError
    bad.usage = mock.MagicMock(input_tokens=1, output_tokens=1)
    bad.stop_reason = "end_turn"
    client = mock.MagicMock()
    client.messages.create.return_value = bad
    p._client = client

    with pytest.raises(LLMBadResponseError):
        p.complete(_req())


def test_openai_unexpected_sdk_exception_becomes_llm_error():
    """A non-taxonomy SDK exception on the API call must become an LLMError so
    run_with_fallback fails over instead of crashing."""
    from core.llm.providers.openai_provider import OpenAIProvider

    p = OpenAIProvider.__new__(OpenAIProvider)
    p._available = True
    # SDK exception classes that are NOT in the taxonomy (so the specific
    # excepts don't match); use real exception types as the mocked attrs.
    p._openai_mod = mock.MagicMock(
        RateLimitError=type("RL", (Exception,), {}),
        APITimeoutError=type("TO", (Exception,), {}),
        AuthenticationError=type("AU", (Exception,), {}),
    )
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = ValueError("connection reset")
    p._client = client

    with pytest.raises(LLMError):
        p.complete(_req())
