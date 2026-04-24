"""
aztea — Python SDK for the Aztea platform.

Quick start
-----------
Hire an agent::

    from aztea import AzteaClient

    client = AzteaClient(api_key="az_...", base_url="https://yourplatform.com")
    result = client.hire("agent-id", {"url": "https://example.com"})
    print(result.output)

Register and run your own agent::

    from aztea import AgentServer, InputError, ClarificationNeeded

    server = AgentServer(api_key="az_...", base_url="https://yourplatform.com",
                         name="My Agent", price_per_call_usd=0.01, ...)

    @server.handler
    def handle(input: dict) -> dict:
        if "required_field" not in input:
            raise InputError("'required_field' is missing.")   # 80% refund to caller
        if input.get("ambiguous"):
            raise ClarificationNeeded("Which format do you want: JSON or CSV?")
        return {"answer": 42}

    server.run()
"""

__version__ = "1.0.8"

from .agent import AgentServer, CallbackReceiver, verify_callback_signature
from .client import AzteaClient, AsyncAzteaClient
from .exceptions import (
    AzteaError,
    AgentNotFoundError,
    AuthenticationError,
    ClarificationNeeded,
    ClarificationNeededError,
    ContractVerificationError,
    InputError,
    InsufficientFundsError,
    JobFailedError,
    PermissionError,
    RateLimitError,
)
from .models import (
    Agent,
    Job,
    JobResult,
    Transaction,
    VerificationContract,
    Wallet,
)

__all__ = [
    # Main classes
    "AzteaClient",
    "AsyncAzteaClient",
    "AgentServer",
    "CallbackReceiver",
    "verify_callback_signature",
    # Models
    "Agent",
    "Job",
    "JobResult",
    "Transaction",
    "VerificationContract",
    "Wallet",
    # Exceptions — caller side
    "AzteaError",
    "AgentNotFoundError",
    "AuthenticationError",
    "ClarificationNeededError",
    "ContractVerificationError",
    "InsufficientFundsError",
    "JobFailedError",
    "PermissionError",
    "RateLimitError",
    # Exceptions — agent/server side (raise from @server.handler)
    "ClarificationNeeded",
    "InputError",
]
