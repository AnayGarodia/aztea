from __future__ import annotations

import os
from typing import Any

from ..base import CompletionRequest, LLMResponse, Usage
from ..errors import LLMAuthError, LLMBadResponseError, LLMRateLimitError


class CohereProvider:
    name = "cohere"

    def __init__(self) -> None:
        self._client: Any = None
        self._available = False
        api_key = os.environ.get("COHERE_API_KEY", "").strip()
        if not api_key:
            return
        try:
            import cohere
            self._client = cohere.ClientV2(api_key=api_key)
            self._available = True
        except ImportError:
            pass

    def is_available(self) -> bool:
        return self._available

    def complete(self, req: CompletionRequest) -> LLMResponse:
        if not self._available:
            raise LLMBadResponseError(self.name, req.model, "Cohere provider not available.", None)
        import cohere

        messages = [{"role": m.role, "content": m.content} for m in req.messages]
        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": messages,
        }
        if req.max_tokens is not None:
            kwargs["max_tokens"] = req.max_tokens
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature

        try:
            resp = self._client.chat(**kwargs)
        except cohere.errors.TooManyRequestsError as exc:
            raise LLMRateLimitError(self.name, req.model, str(exc), exc) from exc
        except cohere.errors.UnauthorizedError as exc:
            raise LLMAuthError(self.name, req.model, str(exc), exc) from exc
        except Exception as exc:
            raise LLMBadResponseError(self.name, req.model, str(exc), exc) from exc

        try:
            text = resp.message.content[0].text or ""
        except Exception as exc:
            raise LLMBadResponseError(self.name, req.model, "Unexpected Cohere response shape.", exc) from exc

        usage = Usage(
            prompt_tokens=getattr(resp.usage, "billed_units", None) and getattr(resp.usage.billed_units, "input_tokens", 0) or 0,
            completion_tokens=getattr(resp.usage, "billed_units", None) and getattr(resp.usage.billed_units, "output_tokens", 0) or 0,
        )
        return LLMResponse(
            text=text,
            model=req.model,
            provider=self.name,
            usage=usage,
            finish_reason=resp.finish_reason or "stop",
        )
