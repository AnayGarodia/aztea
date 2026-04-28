"""Core LLM abstractions: request/response types and the provider protocol.

All built-in agents and judges use these types exclusively. Concrete provider
implementations live in ``core/llm/providers/`` and register themselves via
``core.llm.registry.register_provider``.

Critical usage notes:
- Always read the response via ``LLMResponse.text``, never ``.content``.
  ``.content`` does not exist on this class; using it silently returns ``None``
  at runtime and causes agents to produce empty output without raising.
- When calling ``run_with_fallback``, do NOT set ``model=`` on the request.
  The fallback chain in ``core.llm.registry`` selects the model. Pass
  ``model=""`` or omit it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant"]


@dataclass
class Message:
    """A single chat message with a role and text content."""
    role: Role
    content: str


@dataclass
class CompletionRequest:
    """Parameters for a single LLM completion call.

    ``model`` is typically set by the provider at dispatch time when using
    ``run_with_fallback`` — callers should leave it as an empty string or
    omit it rather than hardcoding a model name.
    """
    model: str
    messages: list[Message]
    temperature: float = 0.0
    max_tokens: int | None = None
    json_mode: bool = False
    stop: list[str] | None = None
    timeout_seconds: float = 60.0


@dataclass
class Usage:
    """Token usage reported by the provider."""
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class LLMResponse:
    """The result of a completed LLM call.

    Always access the generated text via ``.text`` — there is no ``.content``
    attribute on this class.
    """
    text: str
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"


class LLMProvider(Protocol):
    """Interface every provider implementation must satisfy."""
    name: str

    def is_available(self) -> bool:
        """Return True if the provider is configured (API key present, etc.)."""
        ...

    def complete(self, req: CompletionRequest) -> LLMResponse:
        """Execute a completion and return a response, or raise an LLMError subclass."""
        ...
