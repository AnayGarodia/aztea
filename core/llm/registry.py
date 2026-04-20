from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import LLMProvider

_PROVIDERS: dict[str, "LLMProvider"] = {}

_DEFAULT_CHAIN = [
    "groq:llama-3.3-70b-versatile",
    "openai:gpt-4o-mini",
    "anthropic:claude-sonnet-4-6",
]


def _build_default_chain() -> list[str]:
    raw = os.environ.get("AZTEA_LLM_DEFAULT_CHAIN", "").strip()
    if not raw:
        raw = os.environ.get("AGENTMARKET_LLM_DEFAULT_CHAIN", "").strip()
    if raw:
        specs = [s.strip() for s in raw.split(",") if s.strip()]
        if specs:
            return specs
    return list(_DEFAULT_CHAIN)


DEFAULT_CHAIN: list[str] = _build_default_chain()


def register_provider(provider: "LLMProvider") -> None:
    _PROVIDERS[provider.name] = provider


def get_provider(name: str) -> "LLMProvider":
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise KeyError(f"LLM provider '{name}' not registered. Known: {list(_PROVIDERS)}")


def resolve(spec: str) -> tuple["LLMProvider", str]:
    if ":" in spec:
        provider_name, model = spec.split(":", 1)
    else:
        provider_name, model = "groq", spec
    try:
        provider = get_provider(provider_name)
    except KeyError:
        raise ValueError(f"Unknown LLM provider '{provider_name}' in spec '{spec}'.")
    return provider, model


def _bootstrap() -> None:
    from .providers.groq_provider import GroqProvider
    from .providers.openai_provider import OpenAIProvider
    from .providers.anthropic_provider import AnthropicProvider
    register_provider(GroqProvider())
    register_provider(OpenAIProvider())
    register_provider(AnthropicProvider())


_bootstrap()
