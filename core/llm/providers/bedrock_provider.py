from __future__ import annotations

import json
import os
from typing import Any

from ..base import CompletionRequest, LLMResponse, Usage
from ..errors import LLMAuthError, LLMBadResponseError, LLMRateLimitError, LLMTimeoutError


class BedrockProvider:
    """AWS Bedrock provider via boto3 converse API (supports all Bedrock models)."""

    name = "bedrock"

    def __init__(self) -> None:
        self._client: Any = None
        self._available = False
        region = os.environ.get("AWS_BEDROCK_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")).strip()
        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
        # Also works with IAM roles (no explicit keys needed in that case)
        try:
            import boto3
            kwargs: dict[str, Any] = {"region_name": region, "service_name": "bedrock-runtime"}
            if access_key and secret_key:
                kwargs["aws_access_key_id"] = access_key
                kwargs["aws_secret_access_key"] = secret_key
                session_token = os.environ.get("AWS_SESSION_TOKEN", "").strip()
                if session_token:
                    kwargs["aws_session_token"] = session_token
            self._client = boto3.client(**kwargs)
            self._available = True
        except ImportError:
            pass
        except Exception:
            pass

    def is_available(self) -> bool:
        return self._available

    def complete(self, req: CompletionRequest) -> LLMResponse:
        if not self._available:
            raise LLMBadResponseError(self.name, req.model, "Bedrock provider not available.", None)

        import botocore.exceptions

        messages = []
        system_parts: list[dict] = []
        for m in req.messages:
            if m.role == "system":
                system_parts.append({"text": m.content})
            else:
                messages.append({"role": m.role, "content": [{"text": m.content}]})

        inference_config: dict[str, Any] = {}
        if req.max_tokens is not None:
            inference_config["maxTokens"] = req.max_tokens
        if req.temperature is not None:
            inference_config["temperature"] = req.temperature
        if req.stop:
            inference_config["stopSequences"] = req.stop if isinstance(req.stop, list) else [req.stop]

        converse_kwargs: dict[str, Any] = {
            "modelId": req.model,
            "messages": messages,
        }
        if system_parts:
            converse_kwargs["system"] = system_parts
        if inference_config:
            converse_kwargs["inferenceConfig"] = inference_config

        try:
            resp = self._client.converse(**converse_kwargs)
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ThrottlingException", "TooManyRequestsException"):
                raise LLMRateLimitError(self.name, req.model, str(exc), exc) from exc
            if code in ("AccessDeniedException", "UnauthorizedException"):
                raise LLMAuthError(self.name, req.model, str(exc), exc) from exc
            raise LLMBadResponseError(self.name, req.model, str(exc), exc) from exc
        except Exception as exc:
            raise LLMBadResponseError(self.name, req.model, str(exc), exc) from exc

        try:
            text = resp["output"]["message"]["content"][0]["text"]
        except Exception as exc:
            raise LLMBadResponseError(self.name, req.model, "Unexpected Bedrock response shape.", exc) from exc

        token_usage = resp.get("usage", {})
        usage = Usage(
            prompt_tokens=token_usage.get("inputTokens", 0),
            completion_tokens=token_usage.get("outputTokens", 0),
        )
        finish_reason = resp.get("stopReason", "stop")
        return LLMResponse(
            text=text,
            model=req.model,
            provider=self.name,
            usage=usage,
            finish_reason=finish_reason,
        )
