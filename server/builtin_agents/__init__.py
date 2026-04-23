"""Built-in agent definitions (IDs, endpoint maps, registration payloads)."""

from server.builtin_agents.constants import (
    BUILTIN_AGENT_IDS,
    BUILTIN_ENDPOINT_TO_AGENT_ID,
    BUILTIN_INTERNAL_ENDPOINTS,
    BUILTIN_LEGACY_ROUTE_ENDPOINTS,
    BUILTIN_WORKER_OWNER_ID,
    CURATED_BUILTIN_AGENT_IDS,
    CURATED_PUBLIC_BUILTIN_AGENT_IDS,
)
from server.builtin_agents.specs import builtin_agent_specs

__all__ = [
    "BUILTIN_AGENT_IDS",
    "BUILTIN_ENDPOINT_TO_AGENT_ID",
    "BUILTIN_INTERNAL_ENDPOINTS",
    "BUILTIN_LEGACY_ROUTE_ENDPOINTS",
    "BUILTIN_WORKER_OWNER_ID",
    "CURATED_BUILTIN_AGENT_IDS",
    "CURATED_PUBLIC_BUILTIN_AGENT_IDS",
    "builtin_agent_specs",
]
