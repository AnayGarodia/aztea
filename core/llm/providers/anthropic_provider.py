from __future__ import annotations

import json
import os
from typing import Any

from ..base import CompletionRequest, LLMResponse, Usage
from ..errors import (
    LLMAuthError,
    LLMBadResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
)

_JSON_SYSTEM_INJECT = (
    "You must respond with a single valid JSON object and nothing else. "
    "No prose, no markdown fences, no explanation."
)
_DEFAULT_MAX_TOKENS = 4096
_BAD_RESPONSE_PREVIEW_CHARS = 200


def _split_messages(req: CompletionRequest) -> tuple[list[str], list[dict[str, str]]]:
    """Pure: separate system messages (collected) from user/assistant messages."""
    system_parts: list[str] = []
    user_messages: list[dict[str, str]] = []
    if req.json_mode:
        system_parts.append(_JSON_SYSTEM_INJECT)
    for msg in req.messages:
        if msg.role == "system":
            system_parts.append(msg.content)
        else:
            user_messages.append({"role": msg.role, "content": msg.content})
    return system_parts, user_messages


def _validate_json_mode(text: str, model: str) -> None:
    """Pure: raise LLMBadResponseError if json_mode is set and the text isn't valid JSON."""
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMBadResponseError(
            "anthropic",
            model,
            f"json_mode=True but response is not valid JSON: {text[:_BAD_RESPONSE_PREVIEW_CHARS]}",
            exc,
        ) from exc


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        self._client: Any = None
        self._anthropic_mod: Any = None
        self._available = False
        try:
            import anthropic as _anthropic_mod
            from anthropic import Anthropic
        except ImportError:
            return
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            return
        self._client = Anthropic(api_key=key)
        self._anthropic_mod = _anthropic_mod
        self._available = True

    def is_available(self) -> bool:
        return self._available

    def _build_kwargs(self, req: CompletionRequest) -> dict[str, Any]:
        """Pure: shape ``CompletionRequest`` into the kwargs the Anthropic SDK expects."""
        system_parts, user_messages = _split_messages(req)
        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": user_messages,
            "max_tokens": req.max_tokens if req.max_tokens is not None else _DEFAULT_MAX_TOKENS,
            "temperature": req.temperature,
            "timeout": req.timeout_seconds,
        }
        if system_parts:
            kwargs["system"] = "\n".join(system_parts)
        if req.stop:
            kwargs["stop_sequences"] = req.stop
        return kwargs

    def _invoke(self, req: CompletionRequest, kwargs: dict[str, Any]) -> Any:
        """Side-effect: call the Anthropic SDK and translate vendor exceptions to our taxonomy."""
        try:
            return self._client.messages.create(**kwargs)
        except self._anthropic_mod.RateLimitError as exc:
            raise LLMRateLimitError("anthropic", req.model, str(exc), exc) from exc
        except self._anthropic_mod.APITimeoutError as exc:
            raise LLMTimeoutError("anthropic", req.model, str(exc), exc) from exc
        except self._anthropic_mod.AuthenticationError as exc:
            raise LLMAuthError("anthropic", req.model, str(exc), exc) from exc

    def complete(self, req: CompletionRequest) -> LLMResponse:
        """Side-effect: chat completion via the Anthropic Messages API.

        Why: split into ``_build_kwargs`` / ``_invoke`` so the kwargs shape
        is testable in isolation and the vendor-error translation lives in
        a single, narrow boundary.
        """
        response = self._invoke(req, self._build_kwargs(req))
        text = (response.content[0].text if response.content else "").strip()
        if req.json_mode:
            _validate_json_mode(text, req.model)
        usage = Usage(
            prompt_tokens=response.usage.input_tokens if response.usage else 0,
            completion_tokens=response.usage.output_tokens if response.usage else 0,
        )
        return LLMResponse(
            text=text,
            model=req.model,
            provider="anthropic",
            usage=usage,
            finish_reason=response.stop_reason or "stop",
        )
