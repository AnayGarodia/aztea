from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


class AzteaError(Exception):
    """Base SDK error."""


@dataclass
class APIError(AzteaError):
    status_code: int
    message: str
    detail: Any
    body: Any

    def __str__(self) -> str:
        return f"{self.status_code}: {self.message}"


class UnauthorizedError(APIError):
    pass


class AuthenticationError(UnauthorizedError):
    pass


class ForbiddenError(APIError):
    pass


class PermissionError(ForbiddenError):
    pass


class NotFoundError(APIError):
    pass


class AgentNotFoundError(NotFoundError):
    def __init__(self, agent_id: str | None = None, message: str | None = None, detail: Any = None, body: Any = None) -> None:
        if agent_id is not None and message is None:
            message = f"Agent '{agent_id}' not found."
        super().__init__(404, message or "Agent not found.", detail, body)
        self.agent_id = agent_id


class ConflictError(APIError):
    pass


class UnprocessableEntityError(APIError):
    pass


class UpstreamError(APIError):
    pass


class InsufficientBalanceError(APIError):
    @property
    def balance_cents(self) -> int | None:
        if isinstance(self.detail, dict):
            raw = self.detail.get("balance_cents")
            if isinstance(raw, int):
                return raw
        return None

    @property
    def required_cents(self) -> int | None:
        if isinstance(self.detail, dict):
            raw = self.detail.get("required_cents")
            if isinstance(raw, int):
                return raw
        return None


class ClaimLostError(APIError):
    pass


class RateLimitError(APIError):
    @property
    def retry_after(self) -> int:
        if isinstance(self.detail, dict):
            raw = self.detail.get("retry_after_seconds")
            if isinstance(raw, int):
                return raw
        if isinstance(self.body, dict):
            raw = self.body.get("retry_after_seconds")
            if isinstance(raw, int):
                return raw
        return 60


class JobTimeoutError(AzteaError):
    pass


class JobFailedError(AzteaError):
    def __init__(self, message: str, output: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.output = output or {}


class ContractVerificationError(AzteaError):
    def __init__(self, failures: list[str]) -> None:
        super().__init__("Contract verification failed: " + "; ".join(failures))
        self.failures = failures


class ClarificationNeededError(AzteaError):
    def __init__(self, question: str, job_id: str) -> None:
        super().__init__(f"Agent needs clarification: {question}")
        self.question = question
        self.job_id = job_id


class InputError(Exception):
    def __init__(self, message: str, refund_fraction: float = 0.8) -> None:
        super().__init__(message)
        self.refund_fraction = max(0.0, min(1.0, float(refund_fraction)))


class ClarificationNeeded(Exception):
    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


InsufficientFundsError = InsufficientBalanceError


def _extract_response_body(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _extract_detail(body: Any) -> Any:
    if isinstance(body, dict) and "error" in body and "message" in body:
        details = body.get("details")
        if details is None:
            details = body.get("data")
        return details if isinstance(details, dict) else body
    if isinstance(body, dict) and body.get("error") == "rate_limit_exceeded":
        return {"retry_after_seconds": body.get("retry_after_seconds")}
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return body


def raise_for_error_response(response: requests.Response) -> None:
    if response.ok:
        return

    body = _extract_response_body(response)
    detail = _extract_detail(body)
    if isinstance(body, dict) and "message" in body:
        message = str(body.get("message") or "")
    else:
        message = str(detail)
    code = response.status_code

    if code == 401:
        raise UnauthorizedError(code, message, detail, body)
    if code == 402:
        raise InsufficientBalanceError(code, message, detail, body)
    if code == 403:
        raise ForbiddenError(code, message, detail, body)
    if code == 404:
        raise NotFoundError(code, message, detail, body)
    if code == 429:
        raise RateLimitError(code, message, detail, body)
    if code == 409:
        if "claim" in message.lower():
            raise ClaimLostError(code, message, detail, body)
        raise ConflictError(code, message, detail, body)
    if code == 410:
        raise ClaimLostError(code, message, detail, body)
    if code == 422:
        raise UnprocessableEntityError(code, message, detail, body)
    if code in {502, 503}:
        raise UpstreamError(code, message, detail, body)
    raise APIError(code, message, detail, body)
