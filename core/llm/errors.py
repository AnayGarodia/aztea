from __future__ import annotations


class LLMError(Exception):
    def __init__(
        self,
        provider: str,
        model: str,
        message: str,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(f"[{provider}:{model}] {message}")
        self.provider = provider
        self.model = model
        self.cause = cause


class LLMRateLimitError(LLMError):
    def __init__(self, *args, retry_after_seconds: int | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class LLMTimeoutError(LLMError):
    pass


class LLMAuthError(LLMError):
    pass


class LLMBadResponseError(LLMError):
    pass
