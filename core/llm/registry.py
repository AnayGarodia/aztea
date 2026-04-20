from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import LLMProvider

_PROVIDERS: dict[str, "LLMProvider"] = {}

_DEFAULT_CHAIN = [
    "groq:llama-3.3-70b-versatile",
    "openai:gpt-4o-mini",
    "anthropic:claude-sonnet-4-6",
]

_PROVIDER_ALIASES = {
    "xai": "grok",
    "moonshot": "kimi",
    "google": "gemini",
    "googleai": "gemini",
    "vertexai": "gemini",
    "cohere-compat": "cohere",
    "aws": "bedrock",
    "amazon": "bedrock",
    "claude": "anthropic",
    "openrouter-ai": "openrouter",
    "together-ai": "together",
    "fireworks-ai": "fireworks",
    "huggingface": "huggingface",
    "hf": "huggingface",
    "nvidianim": "nvidia",
    "nim": "nvidia",
}


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


def _provider_env_prefix(provider_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", provider_name.strip().lower()).strip("_")
    return slug.upper()


def _register_dynamic_openai_compatible_provider(provider_name: str) -> "LLMProvider" | None:
    normalized = provider_name.strip().lower()
    if not normalized:
        return None
    if normalized in _PROVIDERS:
        return _PROVIDERS[normalized]

    from .providers.openai_compatible_provider import OpenAICompatibleProvider

    prefix = _provider_env_prefix(normalized)
    specific_api_env = f"{prefix}_API_KEY"
    specific_base_env = f"{prefix}_BASE_URL"
    specific_api_key = os.environ.get(specific_api_env, "").strip()
    specific_base_url = os.environ.get(specific_base_env, "").strip()
    if specific_api_key and specific_base_url:
        provider = OpenAICompatibleProvider(
            name=normalized,
            api_key_env=specific_api_env,
            base_url_env=specific_base_env,
            default_base_url="",
        )
        register_provider(provider)
        return provider

    generic_api_key = os.environ.get("OPENAI_COMPAT_API_KEY", "").strip()
    generic_base_url = os.environ.get("OPENAI_COMPAT_BASE_URL", "").strip()
    if generic_api_key and generic_base_url:
        provider = OpenAICompatibleProvider(
            name=normalized,
            api_key_env="OPENAI_COMPAT_API_KEY",
            base_url_env="OPENAI_COMPAT_BASE_URL",
            default_base_url="",
        )
        register_provider(provider)
        return provider
    return None


def resolve(spec: str) -> tuple["LLMProvider", str]:
    if ":" in spec:
        provider_name, model = spec.split(":", 1)
    else:
        provider_name, model = "groq", spec
    provider_name = _PROVIDER_ALIASES.get(provider_name.strip().lower(), provider_name.strip().lower())
    model = model.strip()
    if not model:
        raise ValueError(f"LLM model is required in spec '{spec}'.")
    try:
        provider = get_provider(provider_name)
    except KeyError:
        provider = _register_dynamic_openai_compatible_provider(provider_name)
        if provider is None:
            raise ValueError(
                "Unknown LLM provider "
                f"'{provider_name}' in spec '{spec}'. Configure "
                f"{_provider_env_prefix(provider_name)}_API_KEY and "
                f"{_provider_env_prefix(provider_name)}_BASE_URL, or use OPENAI_COMPAT_API_KEY "
                "with OPENAI_COMPAT_BASE_URL."
            )
    return provider, model


def list_providers() -> list[dict]:
    """Return all registered providers with availability status."""
    result = []
    for name, provider in sorted(_PROVIDERS.items()):
        result.append({
            "name": name,
            "available": provider.is_available(),
            "kind": type(provider).__name__,
        })
    return result


def _bootstrap() -> None:
    from .providers.groq_provider import GroqProvider
    from .providers.openai_provider import OpenAIProvider
    from .providers.anthropic_provider import AnthropicProvider
    from .providers.openai_compatible_provider import OpenAICompatibleProvider
    from .providers.cohere_provider import CohereProvider
    from .providers.bedrock_provider import BedrockProvider

    register_provider(GroqProvider())
    register_provider(OpenAIProvider())
    register_provider(AnthropicProvider())
    register_provider(CohereProvider())
    register_provider(BedrockProvider())

    _COMPAT_PROVIDERS = [
        ("grok",        "XAI_API_KEY",         "XAI_BASE_URL",         "https://api.x.ai/v1"),
        ("kimi",        "KIMI_API_KEY",         "KIMI_BASE_URL",        "https://api.moonshot.ai/v1"),
        ("gemini",      "GEMINI_API_KEY",       "GEMINI_BASE_URL",      "https://generativelanguage.googleapis.com/v1beta/openai/"),
        ("mistral",     "MISTRAL_API_KEY",      "MISTRAL_BASE_URL",     "https://api.mistral.ai/v1"),
        ("together",    "TOGETHER_API_KEY",     "TOGETHER_BASE_URL",    "https://api.together.xyz/v1"),
        ("fireworks",   "FIREWORKS_API_KEY",    "FIREWORKS_BASE_URL",   "https://api.fireworks.ai/inference/v1"),
        ("deepseek",    "DEEPSEEK_API_KEY",     "DEEPSEEK_BASE_URL",    "https://api.deepseek.com"),
        ("perplexity",  "PERPLEXITY_API_KEY",   "PERPLEXITY_BASE_URL",  "https://api.perplexity.ai"),
        ("cerebras",    "CEREBRAS_API_KEY",     "CEREBRAS_BASE_URL",    "https://api.cerebras.ai/v1"),
        ("openrouter",  "OPENROUTER_API_KEY",   "OPENROUTER_BASE_URL",  "https://openrouter.ai/api/v1"),
        ("sambanova",   "SAMBANOVA_API_KEY",    "SAMBANOVA_BASE_URL",   "https://api.sambanova.ai/v1"),
        ("novita",      "NOVITA_API_KEY",       "NOVITA_BASE_URL",      "https://api.novita.ai/v3/openai"),
        ("ai21",        "AI21_API_KEY",         "AI21_BASE_URL",        "https://api.ai21.com/studio/v1"),
        ("deepinfra",   "DEEPINFRA_API_KEY",    "DEEPINFRA_BASE_URL",   "https://api.deepinfra.com/v1/openai"),
        ("hyperbolic",  "HYPERBOLIC_API_KEY",   "HYPERBOLIC_BASE_URL",  "https://api.hyperbolic.xyz/v1"),
        ("anyscale",    "ANYSCALE_API_KEY",     "ANYSCALE_BASE_URL",    "https://api.endpoints.anyscale.com/v1"),
        ("octoai",      "OCTOAI_API_KEY",       "OCTOAI_BASE_URL",      "https://text.octoai.run/v1"),
        ("nvidia",      "NVIDIA_API_KEY",       "NVIDIA_BASE_URL",      "https://integrate.api.nvidia.com/v1"),
        ("predibase",   "PREDIBASE_API_KEY",    "PREDIBASE_BASE_URL",   "https://serving.app.predibase.com/v1"),
        ("huggingface", "HUGGINGFACE_API_KEY",  "HUGGINGFACE_BASE_URL", "https://api-inference.huggingface.co/v1"),
        ("lepton",      "LEPTON_API_KEY",       "LEPTON_BASE_URL",      ""),
        ("azure",       "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_BASE_URL",""),
        ("ollama",      "OLLAMA_API_KEY",       "OLLAMA_BASE_URL",      "http://localhost:11434/v1"),
        ("lmstudio",    "LMSTUDIO_API_KEY",     "LMSTUDIO_BASE_URL",    "http://localhost:1234/v1"),
        ("openai_compat","OPENAI_COMPAT_API_KEY","OPENAI_COMPAT_BASE_URL",""),
    ]

    for name, api_key_env, base_url_env, default_base_url in _COMPAT_PROVIDERS:
        register_provider(
            OpenAICompatibleProvider(
                name=name,
                api_key_env=api_key_env,
                base_url_env=base_url_env,
                default_base_url=default_base_url,
            )
        )


_bootstrap()
