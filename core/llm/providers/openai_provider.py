from __future__ import annotations

import os
from typing import Any

from ..base import CompletionRequest, LLMResponse, Usage
from ..errors import LLMAuthError, LLMRateLimitError, LLMTimeoutError


class OpenAIProvider:
    name = "openai"

    def __init__(self) -> None:
        self._client: Any = None
        self._openai_mod: Any = None
        self._available = False
        try:
            import openai as _openai_mod
            from openai import OpenAI
        except ImportError:
            return
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            return
        self._client = OpenAI(api_key=key)
        self._openai_mod = _openai_mod
        self._available = True

    def is_available(self) -> bool:
        return self._available

    def complete(self, req: CompletionRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": req.model,
            "temperature": req.temperature,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "timeout": req.timeout_seconds,
        }
        if req.max_tokens is not None:
            kwargs["max_tokens"] = req.max_tokens
        if req.stop:
            kwargs["stop"] = req.stop
        if req.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            completion = self._client.chat.completions.create(**kwargs)
        except self._openai_mod.RateLimitError as exc:
            raise LLMRateLimitError("openai", req.model, str(exc), exc) from exc
        except self._openai_mod.APITimeoutError as exc:
            raise LLMTimeoutError("openai", req.model, str(exc), exc) from exc
        except self._openai_mod.AuthenticationError as exc:
            raise LLMAuthError("openai", req.model, str(exc), exc) from exc

        text = (completion.choices[0].message.content or "").strip()
        raw_usage = completion.usage
        usage = Usage(
            prompt_tokens=raw_usage.prompt_tokens if raw_usage else 0,
            completion_tokens=raw_usage.completion_tokens if raw_usage else 0,
        )
        return LLMResponse(
            text=text,
            model=req.model,
            provider="openai",
            usage=usage,
            finish_reason=completion.choices[0].finish_reason or "stop",
        )
