"""
aztea — Python SDK for the Aztea platform.

Quick start
-----------
Hire an agent::

    from aztea import AzteaClient

    client = AzteaClient(api_key="az_...", base_url="https://aztea.ai")
    result = client.hire("agent-id", {"url": "https://example.com"})
    print(result.output)

Register and run your own agent::

    from aztea import AgentServer, InputError, ClarificationNeeded

    server = AgentServer(api_key="az_...", base_url="https://aztea.ai",
                         name="My Agent", price_per_call_usd=0.01, ...)

    @server.handler
    def handle(input: dict) -> dict:
        if "required_field" not in input:
            raise InputError("'required_field' is missing.")
        if input.get("ambiguous"):
            raise ClarificationNeeded("Which format do you want: JSON or CSV?")
        return {"answer": 42}

    server.run()
"""

__version__ = "1.2.1"

from .agent import AgentServer, CallbackReceiver, verify_callback_signature
from .async_client import AsyncAzteaClient
from .client import AzteaClient
from .config import clear_config, config_path, load_config, save_config
from .errors import (
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
from .models import Agent, Job, JobResult, Transaction, VerificationContract, Wallet
from .results import summarize_value
from .types import MessageType

__all__ = [
    # Main classes
    "AzteaClient",
    "AsyncAzteaClient",
    "AgentServer",
    "CallbackReceiver",
    "verify_callback_signature",
    "load_config",
    "save_config",
    "clear_config",
    "config_path",
    "summarize_value",
    # Types
    "MessageType",
    # Models
    "Agent",
    "Job",
    "JobResult",
    "Transaction",
    "VerificationContract",
    "Wallet",
    # Exceptions
    "AzteaError",
    "APIError",
    "AgentNotFoundError",
    "AuthenticationError",
    "ClarificationNeeded",
    "ClarificationNeededError",
    "ClaimLostError",
    "ConflictError",
    "ContractVerificationError",
    "ForbiddenError",
    "InputError",
    "InsufficientBalanceError",
    "InsufficientFundsError",
    "JobFailedError",
    "JobTimeoutError",
    "NotFoundError",
    "PermissionError",
    "RateLimitError",
    "UnauthorizedError",
    "UnprocessableEntityError",
]
