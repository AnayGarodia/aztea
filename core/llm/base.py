from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant"]


@dataclass
class Message:
    role: Role
    content: str


@dataclass
class CompletionRequest:
    model: str
    messages: list[Message]
    temperature: float = 0.0
    max_tokens: int | None = None
    json_mode: bool = False
    stop: list[str] | None = None
    timeout_seconds: float = 60.0


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"


class LLMProvider(Protocol):
    name: str

    def is_available(self) -> bool: ...
    def complete(self, req: CompletionRequest) -> LLMResponse: ...
