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

    def _build_chat_kwargs(self, req: CompletionRequest) -> dict[str, Any]:
        """Pure: shape ``CompletionRequest`` into Cohere chat-API kwargs."""
        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
        }
        if req.max_tokens is not None:
            kwargs["max_tokens"] = req.max_tokens
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        return kwargs

    def _invoke_chat(self, req: CompletionRequest, kwargs: dict[str, Any]) -> Any:
        """Side-effect: call ``client.chat`` and translate vendor errors to our taxonomy."""
        import cohere
        try:
            return self._client.chat(**kwargs)
        except cohere.errors.TooManyRequestsError as exc:
            raise LLMRateLimitError(self.name, req.model, str(exc), exc) from exc
        except cohere.errors.UnauthorizedError as exc:
            raise LLMAuthError(self.name, req.model, str(exc), exc) from exc
        except Exception as exc:
            raise LLMBadResponseError(self.name, req.model, str(exc), exc) from exc

    @staticmethod
    def _extract_usage(resp: Any) -> Usage:
        """Pure: pull billed_units from a Cohere response, defaulting to zero on missing fields."""
        billed = getattr(resp.usage, "billed_units", None) if resp.usage else None
        return Usage(
            prompt_tokens=(getattr(billed, "input_tokens", 0) if billed else 0) or 0,
            completion_tokens=(getattr(billed, "output_tokens", 0) if billed else 0) or 0,
        )

    def complete(self, req: CompletionRequest) -> LLMResponse:
        """Side-effect: chat completion via the Cohere chat API."""
        if not self._available:
            raise LLMBadResponseError(
                self.name, req.model, "Cohere provider not available.", None,
            )
        resp = self._invoke_chat(req, self._build_chat_kwargs(req))
        try:
            text = resp.message.content[0].text or ""
        except Exception as exc:
            raise LLMBadResponseError(
                self.name, req.model, "Unexpected Cohere response shape.", exc,
            ) from exc
        return LLMResponse(
            text=text,
            model=req.model,
            provider=self.name,
            usage=self._extract_usage(resp),
            finish_reason=resp.finish_reason or "stop",
        )
