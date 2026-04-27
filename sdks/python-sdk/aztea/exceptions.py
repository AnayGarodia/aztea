"""Compatibility shim — exceptions now live in aztea.errors."""

from .errors import (  # noqa: F401
    AzteaError,
    APIError,
    AgentNotFoundError,
    AuthenticationError,
    ClarificationNeeded,
    ClarificationNeededError,
    ClaimLostError,
    ConflictError,
    ContractVerificationError,
    ForbiddenError,
    InputError,
    InsufficientBalanceError,
    InsufficientFundsError,
    JobFailedError,
    JobTimeoutError,
    NotFoundError,
    PermissionError,
    RateLimitError,
    UnauthorizedError,
    UnprocessableEntityError,
)
