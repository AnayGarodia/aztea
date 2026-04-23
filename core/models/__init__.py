"""Pydantic request/response contracts for the HTTP surface.

This package replaces the legacy ``core/models.py``. It is split along the
natural shape of the API:

- ``core_types`` — shared primitives, enums, and reusable field validators
  (e.g. ``JSONObject``, ``CallerContext`` TypedDict, agent-name / URL /
  price validators, onboarding-manifest schema).
- ``job_requests`` — request bodies for ``/jobs/*`` and related worker flows.
- ``messages_ops`` — typed job-message payloads, MCP invocation, registry
  search/call bodies. ``RegistryCallRequest`` enforces payload shape and size
  limits (<= 64 KB, <= 8 levels deep, <= 120 keys, <= 4000 chars per string).
- ``responses`` — response models used for FastAPI ``response_model`` hooks.

Each submodule uses a star import from ``core_types`` so shared types are in
scope. The package ``__init__`` then re-exports every model name so
``from core.models import AgentRegisterRequest`` keeps working.
"""

from core.models.core_types import *  # noqa: F401,F403
from core.models.job_requests import *  # noqa: F401,F403
from core.models.messages_ops import *  # noqa: F401,F403
from core.models.responses import *  # noqa: F401,F403
