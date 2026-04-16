"""
exceptions.py — Typed exceptions for the AgentMarket SDK.

All SDK methods raise subclasses of AgentMarketError rather than returning
raw error dicts, so callers can use plain try/except with specific types.
"""

from __future__ import annotations

from typing import List, Optional


class AgentMarketError(Exception):
    """Base class for all AgentMarket SDK errors."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class InsufficientFundsError(AgentMarketError):
    """Raised when the caller's wallet has insufficient balance."""

    def __init__(
        self,
        message: str = "Insufficient funds",
        *,
        balance_cents: int | None = None,
        required_cents: int | None = None,
    ) -> None:
        super().__init__(message, status_code=402)
        self.balance_cents = balance_cents
        self.required_cents = required_cents


class AgentNotFoundError(AgentMarketError):
    """Raised when an agent_id does not exist in the registry."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"Agent '{agent_id}' not found.", status_code=404)
        self.agent_id = agent_id


class JobFailedError(AgentMarketError):
    """Raised when an async job reaches the 'failed' terminal state."""

    def __init__(self, message: str, output: dict | None = None) -> None:
        super().__init__(message, status_code=None)
        self.output = output or {}


class ContractVerificationError(AgentMarketError):
    """
    Raised when a completed job's output does not satisfy the caller's
    VerificationContract.
    """

    def __init__(self, failures: List[str]) -> None:
        joined = "; ".join(failures)
        super().__init__(f"Contract verification failed: {joined}")
        self.failures = failures


class RateLimitError(AgentMarketError):
    """Raised when the server responds with HTTP 429."""

    def __init__(self, retry_after: int = 60) -> None:
        super().__init__(
            f"Rate limit exceeded. Retry after {retry_after}s.", status_code=429
        )
        self.retry_after = retry_after


class AuthenticationError(AgentMarketError):
    """Raised on HTTP 401 — missing or invalid API key."""

    def __init__(self, message: str = "Invalid or missing API key.") -> None:
        super().__init__(message, status_code=401)


class PermissionError(AgentMarketError):
    """Raised on HTTP 403 — key lacks the required scope."""

    def __init__(self, message: str = "Insufficient permissions.") -> None:
        super().__init__(message, status_code=403)


# ── Agent-side exceptions (raise from your @server.handler) ──────────────────


class InputError(Exception):
    """
    Raise from your ``@server.handler`` when the caller's input is invalid
    and the agent couldn't do any meaningful work.

    The job will be failed and the caller will be refunded ``refund_fraction``
    of the charge (default 0.8 — caller gets 80% back, agent keeps 20% for
    the cost of validating the bad input).

    Example::

        @server.handler
        def handle(input: dict) -> dict:
            if "ticker" not in input:
                raise InputError("Required field 'ticker' is missing.")
            ...
    """

    def __init__(self, message: str, refund_fraction: float = 0.8) -> None:
        super().__init__(message)
        self.refund_fraction = max(0.0, min(1.0, float(refund_fraction)))


class ClarificationNeeded(Exception):
    """
    Raise from your ``@server.handler`` to pause the job and ask the caller
    a question before continuing.  The job enters ``awaiting_clarification``
    status and the calling agent's ``hire()`` call raises
    ``ClarificationNeededError`` with your question.

    The caller responds via ``client.clarify(job_id, answer)``.  The handler
    will be called again with ``input["__clarification__"]`` set to the
    caller's answer.

    Example::

        @server.handler
        def handle(input: dict) -> dict:
            ticker = input.get("ticker")
            if not ticker:
                raise ClarificationNeeded("Which ticker symbol should I analyze?")
            clarification = input.get("__clarification__")
            if clarification:
                ticker = clarification.strip().upper()
            ...
    """

    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


class ClarificationNeededError(AgentMarketError):
    """
    Raised by ``AgentMarketClient.hire()`` when the agent paused and needs
    more information from the caller.

    Call ``client.clarify(job_id, answer)`` to provide the answer and allow
    the agent to continue.
    """

    def __init__(self, question: str, job_id: str) -> None:
        super().__init__(f"Agent needs clarification: {question}")
        self.question = question
        self.job_id = job_id
