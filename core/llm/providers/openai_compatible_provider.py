from __future__ import annotations

import os
from typing import Any

from ..base import CompletionRequest, LLMResponse, Usage
from ..errors import LLMAuthError, LLMBadResponseError, LLMRateLimitError, LLMTimeoutError


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        api_key_env: str,
        base_url_env: str,
        default_base_url: str,
    ) -> None:
        self.name = name
        self._client: Any = None
        self._openai_mod: Any = None
        self._available = False
        self._api_key_env = api_key_env
        self._base_url_env = base_url_env
        self._default_base_url = default_base_url
        try:
            import openai as _openai_mod
            from openai import OpenAI
        except ImportError:
            return
        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            return
        base_url = os.environ.get(base_url_env, "").strip() or default_base_url
        if not base_url:
            return
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._openai_mod = _openai_mod
        self._available = True

    def is_available(self) -> bool:
        return self._available

    def complete(self, req: CompletionRequest) -> LLMResponse:
        """Send a chat completion request to an OpenAI-compatible endpoint and return a normalised LLMResponse."""
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
            raise LLMRateLimitError(self.name, req.model, str(exc), exc) from exc
        except self._openai_mod.APITimeoutError as exc:
            raise LLMTimeoutError(self.name, req.model, str(exc), exc) from exc
        except self._openai_mod.AuthenticationError as exc:
            raise LLMAuthError(self.name, req.model, str(exc), exc) from exc
        except Exception as exc:
            raise LLMBadResponseError(self.name, req.model, str(exc), exc) from exc

        try:
            text = (completion.choices[0].message.content or "").strip()
        except Exception as exc:
            raise LLMBadResponseError(
                self.name,
                req.model,
                "Provider returned an unexpected response shape.",
                exc,
            ) from exc
        raw_usage = completion.usage
        usage = Usage(
            prompt_tokens=raw_usage.prompt_tokens if raw_usage else 0,
            completion_tokens=raw_usage.completion_tokens if raw_usage else 0,
        )
        return LLMResponse(
            text=text,
            model=req.model,
            provider=self.name,
            usage=usage,
            finish_reason=completion.choices[0].finish_reason or "stop",
        )
