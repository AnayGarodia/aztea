"""LLM provider registry: registration, resolution, and the default fallback chain.

This module is the single source of truth for which LLM providers are available
and how a "spec" string is resolved to a (provider, model) pair.

Key concepts
------------
``_PROVIDERS``
    In-memory dict mapping provider name → LLMProvider instance. Populated at
    import time by ``_bootstrap()`` and by ``register_provider()`` for any
    dynamically created OpenAI-compatible providers.

``DEFAULT_CHAIN``
    Ordered list of "provider:model" spec strings used by ``run_with_fallback``
    when no explicit chain is given. Built from the ``AZTEA_LLM_DEFAULT_CHAIN``
    env var (comma-separated); falls back to the hard-coded ``_DEFAULT_CHAIN``
    constant if the env var is absent or empty.

``resolve(spec)``
    Parses a spec string into ``(provider, model)``. Spec format:
    - ``"provider:model"``  — explicit provider + model, e.g. ``"groq:llama-3.3-70b-versatile"``
    - ``"model"``           — bare model name; defaults to the ``groq`` provider
    - Aliases: ``"claude"`` → ``"anthropic"``, ``"gpt"`` → ``"openai"``, etc.
    If the named provider is not in ``_PROVIDERS``, ``resolve`` attempts to
    auto-register it as an OpenAI-compatible provider using the env vars
    ``{PREFIX}_API_KEY`` and ``{PREFIX}_BASE_URL`` (or the generic
    ``OPENAI_COMPAT_*`` fallback). Raises ``ValueError`` if no configuration
    is found.

Adding a new native provider
-----------------------------
1. Create ``core/llm/providers/{name}_provider.py`` implementing ``LLMProvider``.
2. Import and ``register_provider(YourProvider())`` inside ``_bootstrap()`` below.
3. Add its env-var name to CLAUDE.md under "LLM provider system".

Adding a new OpenAI-compatible provider
-----------------------------------------
Add a row to ``_COMPAT_PROVIDERS`` inside ``_bootstrap()``:
``(name, "NAME_API_KEY", "NAME_BASE_URL", "https://api.example.com/v1")``.
No other code changes needed.
"""
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

# Short aliases so callers can write e.g. "claude:..." instead of "anthropic:..."
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
    "gpt": "openai",
    "openrouter-ai": "openrouter",
    "together-ai": "together",
    "fireworks-ai": "fireworks",
    "huggingface": "huggingface",
    "hf": "huggingface",
    "nvidianim": "nvidia",
    "nim": "nvidia",
    "llama": "groq",
}


def _build_default_chain() -> list[str]:
    """Build DEFAULT_CHAIN from env var, falling back to the hard-coded list."""
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
    """Register a provider instance under its ``provider.name`` key.

    Calling this with an already-registered name silently replaces the old
    instance — useful in tests or when re-bootstrapping with different config.
    """
    _PROVIDERS[provider.name] = provider


def get_provider(name: str) -> "LLMProvider":
    """Return the registered provider for ``name``, or raise ``KeyError``."""
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise KeyError(f"LLM provider '{name}' not registered. Known: {list(_PROVIDERS)}")


def _provider_env_prefix(provider_name: str) -> str:
    """Convert a provider name to its uppercase env-var prefix.

    Examples: ``"openai"`` → ``"OPENAI"``, ``"lm-studio"`` → ``"LM_STUDIO"``.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", provider_name.strip().lower()).strip("_")
    return slug.upper()


def _register_dynamic_openai_compatible_provider(provider_name: str) -> "LLMProvider" | None:
    """Attempt to auto-register an unknown provider as an OpenAI-compatible endpoint.

    Checks for ``{PREFIX}_API_KEY`` + ``{PREFIX}_BASE_URL`` env vars first, then
    falls back to the generic ``OPENAI_COMPAT_API_KEY`` / ``OPENAI_COMPAT_BASE_URL``
    pair. Returns ``None`` if neither set of vars is present.
    """
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
    """Parse a spec string and return ``(provider, model)``.

    Spec format:
    - ``"provider:model"`` — e.g. ``"groq:llama-3.3-70b-versatile"``
    - ``"model"``          — bare model name; assumed to be on the ``groq`` provider

    Alias expansion happens before lookup, so ``"claude:claude-3-5-sonnet-20241022"``
    resolves to the ``anthropic`` provider.

    If the provider is not already registered, attempts dynamic registration as an
    OpenAI-compatible endpoint via ``_register_dynamic_openai_compatible_provider``.

    Raises ``ValueError`` if the spec has no model part, or if the provider cannot
    be resolved (not registered and no matching env vars found).
    """
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
    """Register all built-in providers at import time.

    Native providers (groq, openai, anthropic, cohere, bedrock) use their own
    provider classes. Everything else goes through ``OpenAICompatibleProvider``
    with provider-specific env-var names and default base URLs.
    """
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
