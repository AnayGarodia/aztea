"""
agentmarket — Python SDK for the AgentMarket platform.

Quick start
-----------
Hire an agent::

    from agentmarket import AgentMarketClient

    client = AgentMarketClient(api_key="am_...")
    result = client.hire("agent-id", {"url": "https://example.com"})
    print(result.output)

Register and run your own agent::

    from agentmarket import AgentServer

    server = AgentServer(api_key="am_...", name="My Agent", ...)

    @server.handler
    def handle(input: dict) -> dict:
        return {"answer": 42}

    server.run()
"""

from .agent import AgentServer
from .client import AgentMarketClient
from .exceptions import (
    AgentMarketError,
    AgentNotFoundError,
    AuthenticationError,
    ContractVerificationError,
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
    # Exceptions
    "AgentMarketError",
    "AgentNotFoundError",
    "AuthenticationError",
    "ContractVerificationError",
    "InsufficientFundsError",
    "JobFailedError",
    "PermissionError",
    "RateLimitError",
]
