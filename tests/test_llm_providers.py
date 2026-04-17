"""Tests for core/llm/ provider abstraction layer."""
import unittest.mock as mock

import pytest

from core.llm.base import CompletionRequest, LLMResponse, Message, Usage
from core.llm.errors import (
    LLMBadResponseError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_req(**kwargs) -> CompletionRequest:
    defaults = dict(model="test-model", messages=[Message("user", "hello")])
    defaults.update(kwargs)
    return CompletionRequest(**defaults)


def _mock_response(text: str = '{"ok":true}', model: str = "test-model", provider: str = "groq") -> LLMResponse:
    return LLMResponse(text=text, model=model, provider=provider, usage=Usage())


# ---------------------------------------------------------------------------
# Registry: resolve()
# ---------------------------------------------------------------------------

def test_registry_resolves_prefixed_spec():
    from core.llm.registry import resolve
    provider, model = resolve("groq:llama-3.3-70b-versatile")
    assert provider.name == "groq"
    assert model == "llama-3.3-70b-versatile"


def test_registry_resolves_bare_spec_defaults_to_groq():
    from core.llm.registry import resolve
    provider, model = resolve("llama-3.1-70b-versatile")
    assert provider.name == "groq"
    assert model == "llama-3.1-70b-versatile"


def test_registry_raises_on_unknown_provider():
    from core.llm.registry import resolve
    with pytest.raises(ValueError, match="Unknown LLM provider 'martian'"):
        resolve("martian:some-model")


def test_default_chain_env_override(monkeypatch):
    monkeypatch.setenv("AGENTMARKET_LLM_DEFAULT_CHAIN", "openai:gpt-4o-mini,groq:llama-3.3-70b-versatile")
    from core.llm import registry as reg_mod
    chain = reg_mod._build_default_chain()
    assert chain == ["openai:gpt-4o-mini", "groq:llama-3.3-70b-versatile"]


def test_default_chain_env_empty_uses_hardcoded(monkeypatch):
    monkeypatch.setenv("AGENTMARKET_LLM_DEFAULT_CHAIN", "")
    from core.llm import registry as reg_mod
    chain = reg_mod._build_default_chain()
    assert "groq:llama-3.3-70b-versatile" in chain


# ---------------------------------------------------------------------------
# Provider availability
# ---------------------------------------------------------------------------

def test_provider_unavailable_when_sdk_missing(monkeypatch):
    # Make groq un-importable within a fresh provider instance
    import builtins
    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "groq":
            raise ImportError("mocked missing groq")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=_blocking_import):
        from core.llm.providers.groq_provider import GroqProvider
        p = GroqProvider.__new__(GroqProvider)
        p._available = False
        p._client = None
        p._groq_mod = None
        # Manually replicate __init__ logic under mocked import
        try:
            import groq  # noqa: F401 — this will raise
        except ImportError:
            pass
        assert not p.is_available()


def test_provider_unavailable_when_key_missing(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from core.llm.providers.groq_provider import GroqProvider
    p = GroqProvider()
    assert not p.is_available()


def test_openai_provider_unavailable_when_key_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from core.llm.providers.openai_provider import OpenAIProvider
    p = OpenAIProvider()
    assert not p.is_available()


def test_anthropic_provider_unavailable_when_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from core.llm.providers.anthropic_provider import AnthropicProvider
    p = AnthropicProvider()
    assert not p.is_available()


# ---------------------------------------------------------------------------
# run_with_fallback()
# ---------------------------------------------------------------------------

def _make_mock_provider(name: str, available: bool = True,
                        response: LLMResponse | None = None,
                        raises=None) -> mock.MagicMock:
    p = mock.MagicMock()
    p.name = name
    p.is_available.return_value = available
    if raises is not None:
        p.complete.side_effect = raises
    elif response is not None:
        p.complete.return_value = response
    return p


def test_fallback_skips_unavailable_providers(monkeypatch):
    from core.llm import registry as reg_mod
    from core.llm import fallback as fb_mod

    groq_p = _make_mock_provider("groq", available=False)
    openai_p = _make_mock_provider("openai", available=False)
    anthropic_p = _make_mock_provider("anthropic", available=True, response=_mock_response(provider="anthropic"))

    monkeypatch.setattr(reg_mod, "_PROVIDERS", {"groq": groq_p, "openai": openai_p, "anthropic": anthropic_p})

    req = _make_req()
    result = fb_mod.run_with_fallback(req, model_chain=["groq:m1", "openai:m2", "anthropic:m3"])
    assert result.provider == "anthropic"
    groq_p.complete.assert_not_called()
    openai_p.complete.assert_not_called()
    anthropic_p.complete.assert_called_once()


def test_fallback_retries_on_rate_limit(monkeypatch):
    from core.llm import registry as reg_mod
    from core.llm import fallback as fb_mod

    rate_err = LLMRateLimitError("groq", "m1", "rate limited")
    groq_p = _make_mock_provider("groq", available=True, raises=rate_err)
    openai_p = _make_mock_provider("openai", available=True, response=_mock_response(provider="openai"))

    monkeypatch.setattr(reg_mod, "_PROVIDERS", {"groq": groq_p, "openai": openai_p})

    result = fb_mod.run_with_fallback(_make_req(), model_chain=["groq:m1", "openai:m2"])
    assert result.provider == "openai"


def test_fallback_raises_last_error_when_all_fail(monkeypatch):
    from core.llm import registry as reg_mod
    from core.llm import fallback as fb_mod

    err1 = LLMRateLimitError("groq", "m1", "rate limited")
    err2 = LLMTimeoutError("openai", "m2", "timeout")
    groq_p = _make_mock_provider("groq", available=True, raises=err1)
    openai_p = _make_mock_provider("openai", available=True, raises=err2)

    monkeypatch.setattr(reg_mod, "_PROVIDERS", {"groq": groq_p, "openai": openai_p})

    with pytest.raises(LLMError):
        fb_mod.run_with_fallback(_make_req(), model_chain=["groq:m1", "openai:m2"])


def test_fallback_raises_generic_when_all_unavailable(monkeypatch):
    from core.llm import registry as reg_mod
    from core.llm import fallback as fb_mod

    groq_p = _make_mock_provider("groq", available=False)
    monkeypatch.setattr(reg_mod, "_PROVIDERS", {"groq": groq_p})

    with pytest.raises(LLMError, match="No available"):
        fb_mod.run_with_fallback(_make_req(), model_chain=["groq:m1"])


# ---------------------------------------------------------------------------
# Provider-specific: JSON mode translation
# ---------------------------------------------------------------------------

def test_anthropic_json_mode_injects_system_prompt():
    from core.llm.providers.anthropic_provider import AnthropicProvider, _JSON_SYSTEM_INJECT

    p = AnthropicProvider.__new__(AnthropicProvider)
    p._available = True
    p._anthropic_mod = mock.MagicMock()

    mock_resp = mock.MagicMock()
    mock_resp.content = [mock.MagicMock(text='{"answer": 42}')]
    mock_resp.usage = mock.MagicMock(input_tokens=10, output_tokens=5)
    mock_resp.stop_reason = "end_turn"

    mock_client = mock.MagicMock()
    mock_client.messages.create.return_value = mock_resp
    p._client = mock_client

    req = _make_req(
        messages=[Message("system", "you are helpful"), Message("user", "respond in JSON")],
        json_mode=True,
    )
    p.complete(req)

    call_kwargs = mock_client.messages.create.call_args[1]
    assert "system" in call_kwargs
    assert _JSON_SYSTEM_INJECT in call_kwargs["system"]


def test_anthropic_splits_system_messages():
    from core.llm.providers.anthropic_provider import AnthropicProvider

    p = AnthropicProvider.__new__(AnthropicProvider)
    p._available = True
    p._anthropic_mod = mock.MagicMock()

    mock_resp = mock.MagicMock()
    mock_resp.content = [mock.MagicMock(text='{"x":1}')]
    mock_resp.usage = mock.MagicMock(input_tokens=5, output_tokens=3)
    mock_resp.stop_reason = "end_turn"

    mock_client = mock.MagicMock()
    mock_client.messages.create.return_value = mock_resp
    p._client = mock_client

    req = _make_req(
        messages=[
            Message("system", "be concise"),
            Message("system", "respond in English"),
            Message("user", "hello"),
        ],
        json_mode=False,
    )
    p.complete(req)

    call_kwargs = mock_client.messages.create.call_args[1]
    # Both system messages should be concatenated into one string
    assert "be concise" in call_kwargs["system"]
    assert "respond in English" in call_kwargs["system"]
    # Non-system messages stay in messages list
    assert call_kwargs["messages"] == [{"role": "user", "content": "hello"}]


def test_groq_json_mode_passes_response_format():
    from core.llm.providers.groq_provider import GroqProvider

    p = GroqProvider.__new__(GroqProvider)
    p._available = True
    p._groq_mod = mock.MagicMock()

    mock_resp = mock.MagicMock()
    mock_resp.choices = [mock.MagicMock()]
    mock_resp.choices[0].message.content = '{"result": "ok"}'
    mock_resp.choices[0].finish_reason = "stop"
    mock_resp.usage = mock.MagicMock(prompt_tokens=5, completion_tokens=3)

    mock_client = mock.MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp
    p._client = mock_client

    req = _make_req(json_mode=True)
    p.complete(req)

    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert call_kwargs.get("response_format") == {"type": "json_object"}


def test_anthropic_bad_json_response_raises():
    from core.llm.providers.anthropic_provider import AnthropicProvider

    p = AnthropicProvider.__new__(AnthropicProvider)
    p._available = True
    p._anthropic_mod = mock.MagicMock()

    mock_resp = mock.MagicMock()
    mock_resp.content = [mock.MagicMock(text="not json at all")]
    mock_resp.usage = mock.MagicMock(input_tokens=5, output_tokens=3)
    mock_resp.stop_reason = "end_turn"

    mock_client = mock.MagicMock()
    mock_client.messages.create.return_value = mock_resp
    p._client = mock_client

    req = _make_req(json_mode=True)
    with pytest.raises(LLMBadResponseError):
        p.complete(req)
