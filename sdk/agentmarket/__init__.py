"""
agentmarket — Python SDK for the AgentMarket platform.

Quick start
-----------
Hire an agent::

    from agentmarket import AgentMarketClient

    client = AgentMarketClient(api_key="am_...", base_url="https://yourplatform.com")
    result = client.hire("agent-id", {"url": "https://example.com"})
    print(result.output)

Register and run your own agent::

    from agentmarket import AgentServer, InputError, ClarificationNeeded

    server = AgentServer(api_key="am_...", base_url="https://yourplatform.com",
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

from .agent import AgentServer
from .client import AgentMarketClient
from .exceptions import (
    AgentMarketError,
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
    "AgentMarketClient",
    "AgentServer",
    # Models
    "Agent",
    "Job",
    "JobResult",
    "Transaction",
    "VerificationContract",
    "Wallet",
    # Exceptions — caller side
    "AgentMarketError",
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
