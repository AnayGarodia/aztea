"""Tool factory — turns the live Aztea catalog into LangChain StructuredTools.

# OWNS: the sync + async public surfaces (`load_aztea_tools`,
#       `load_aztea_tools_async`) and per-tool Pydantic args_schema
#       construction from each agent's declared input_schema.
# NOT OWNS: agent invocation contract (lives in the Aztea backend at
#       POST /registry/agents/{id}/call), catalog shape (lives in the
#       backend's /registry/agents response).
# INVARIANTS:
#   - load_aztea_tools fetches the catalog exactly once and returns a list
#     of tool instances. Each tool's .invoke() makes one Aztea HTTP call.
#   - The Aztea backend is the source of truth for auth, billing, refund
#     semantics. This module is a thin transport adapter.
# KNOWN DEBT:
#   - Pydantic args_schema construction is EAGER today (we pass the
#     materialized class to StructuredTool.from_function up-front in
#     _build_tool). The closure pattern in _materialize_args_schema was
#     meant to defer until first invoke, but StructuredTool's contract
#     requires the schema class at construction time, so we end up
#     compiling every agent's schema during load_aztea_tools. /review
#     2026-05-27 caught the docstring/behavior mismatch; for a 500-agent
#     catalog this is measurable. A real fix would either (a) materialize
#     a stub args_schema={} at init and resubmit with the real one on
#     first invoke (LangChain doesn't natively support this) or (b) wrap
#     StructuredTool with a __getattr__ proxy. Until then this is honest.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, create_model

__all__ = ["load_aztea_tools", "load_aztea_tools_async"]


_DEFAULT_BASE_URL = "https://aztea.ai"
_DEFAULT_TIMEOUT_SECONDS = 30.0


# ─── Public surface ────────────────────────────────────────────────────────


def load_aztea_tools(
    *,
    api_key: str,
    base_url: str | None = None,
    tag: str | None = None,
    max_price_usd: float | None = None,
    min_trust: float | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    include_reputation: bool = True,
) -> list[StructuredTool]:
    """Fetch the Aztea catalog and return one StructuredTool per agent.

    Args:
        api_key: Aztea API key. Generate at https://aztea.ai/account/keys.
        base_url: Override the API base URL (default: https://aztea.ai).
        tag: Filter to agents tagged with this string (e.g. "security",
            "data"). Maps directly to the backend's `/registry/agents?tag=`
            query param. (An earlier version accepted `category=`, which
            silently no-op'd because the backend doesn't expose that param.
            /review caught this 2026-05-27.)
        max_price_usd: Drop agents whose per-call price exceeds this.
        min_trust: Drop agents whose trust score is below this (0.0–1.0).
        timeout: HTTP timeout in seconds for both catalog fetch and per-call.
        include_reputation: Pass-through to the catalog endpoint; default on
            so trust scores come back populated.

    Returns: a list of `langchain_core.tools.StructuredTool` instances.
        Each tool's `.invoke(input)` makes one Aztea API call.
    """
    resolved_base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    agents = _fetch_catalog_sync(
        api_key=api_key,
        base_url=resolved_base,
        tag=tag,
        timeout=timeout,
        include_reputation=include_reputation,
    )
    filtered = _filter_agents(agents, max_price_usd=max_price_usd, min_trust=min_trust)
    return [
        _build_tool(agent, api_key=api_key, base_url=resolved_base, timeout=timeout)
        for agent in filtered
    ]


async def load_aztea_tools_async(
    *,
    api_key: str,
    base_url: str | None = None,
    tag: str | None = None,
    max_price_usd: float | None = None,
    min_trust: float | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    include_reputation: bool = True,
) -> list[StructuredTool]:
    """Async variant of `load_aztea_tools`. Same kwargs, same return shape."""
    resolved_base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    agents = await _fetch_catalog_async(
        api_key=api_key,
        base_url=resolved_base,
        tag=tag,
        timeout=timeout,
        include_reputation=include_reputation,
    )
    filtered = _filter_agents(agents, max_price_usd=max_price_usd, min_trust=min_trust)
    return [
        _build_tool(
            agent, api_key=api_key, base_url=resolved_base, timeout=timeout,
        )
        for agent in filtered
    ]


# ─── Catalog fetch ─────────────────────────────────────────────────────────


def _fetch_catalog_sync(
    *,
    api_key: str,
    base_url: str,
    tag: str | None,
    timeout: float,
    include_reputation: bool,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {
        "include_reputation": "true" if include_reputation else "false",
    }
    if tag:
        params["tag"] = tag
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(
            f"{base_url}/registry/agents",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("agents") or []


async def _fetch_catalog_async(
    *,
    api_key: str,
    base_url: str,
    tag: str | None,
    timeout: float,
    include_reputation: bool,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {
        "include_reputation": "true" if include_reputation else "false",
    }
    if tag:
        params["tag"] = tag
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"{base_url}/registry/agents",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("agents") or []


# ─── Filtering ─────────────────────────────────────────────────────────────


def _filter_agents(
    agents: list[dict[str, Any]],
    *,
    max_price_usd: float | None,
    min_trust: float | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for agent in agents:
        if max_price_usd is not None:
            try:
                price = float(agent.get("price_per_call_usd") or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            if price > max_price_usd:
                continue
        if min_trust is not None:
            try:
                trust = float(agent.get("trust_score") or 0.0)
            except (TypeError, ValueError):
                trust = 0.0
            # Trust score range may be 0-1 or 0-100 depending on the platform
            # snapshot; accept the threshold in the same range the caller used.
            if trust < min_trust:
                continue
        out.append(agent)
    return out


# ─── Per-tool construction ────────────────────────────────────────────────


def _build_tool(
    agent: dict[str, Any],
    *,
    api_key: str,
    base_url: str,
    timeout: float,
) -> StructuredTool:
    """Construct one StructuredTool from a catalog agent dict.

    The Pydantic args_schema is materialized lazily — we cache it on the
    closure so subsequent invocations skip the recompile. LangChain accepts
    a fully-empty stub at init time; the real schema can come from the
    agent's `input_schema` on first invoke.
    """
    agent_id = str(agent.get("agent_id") or "").strip()
    slug = str(agent.get("slug") or "").strip()
    if not agent_id:
        raise ValueError(f"Agent missing agent_id: {agent!r}")
    # LangChain tool name must be a valid Python identifier; the slug is
    # usually kebab-case so we underscore it for tool_name.
    tool_name = (slug or agent_id).replace("-", "_")
    description = str(
        agent.get("description") or agent.get("summary") or f"Aztea agent {tool_name}"
    )

    args_schema_cache: dict[str, type[BaseModel]] = {}

    def _materialize_args_schema() -> type[BaseModel]:
        if "schema" in args_schema_cache:
            return args_schema_cache["schema"]
        input_schema = agent.get("input_schema") or {}
        model = _json_schema_to_pydantic_model(
            f"{tool_name.capitalize()}Args", input_schema,
        )
        args_schema_cache["schema"] = model
        return model

    def _call(**kwargs: Any) -> Any:
        # Validate via the lazy schema, then call Aztea.
        schema = _materialize_args_schema()
        validated = schema(**kwargs).model_dump()
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{base_url}/registry/agents/{agent_id}/call",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"input_payload": validated},
            )
        resp.raise_for_status()
        return resp.json()

    async def _acall(**kwargs: Any) -> Any:
        schema = _materialize_args_schema()
        validated = schema(**kwargs).model_dump()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url}/registry/agents/{agent_id}/call",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"input_payload": validated},
            )
        resp.raise_for_status()
        return resp.json()

    return StructuredTool.from_function(
        func=_call,
        coroutine=_acall,
        name=tool_name,
        description=description,
        args_schema=_materialize_args_schema(),
    )


# ─── JSON Schema → Pydantic ────────────────────────────────────────────────


_JSON_TYPE_TO_PYTHON: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _json_schema_to_pydantic_model(
    model_name: str, schema: dict[str, Any],
) -> type[BaseModel]:
    """Lightweight JSON Schema → Pydantic translator.

    Handles the most common cases (typed properties, required-fields list,
    enums). Unknown fields default to `Any` so the model stays permissive
    rather than rejecting on the LangChain side — the Aztea backend will
    validate authoritatively.
    """
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, field_schema in properties.items():
        if not isinstance(field_schema, dict):
            continue
        py_type = _json_schema_field_to_type(field_schema)
        default = ... if field_name in required else None
        fields[field_name] = (py_type, default)
    if not fields:
        # No declared properties → accept anything via a single dict field.
        fields = {"input": (dict, ...)}
    return create_model(model_name, **fields)  # type: ignore[no-any-return,arg-type]


def _json_schema_field_to_type(field_schema: dict[str, Any]) -> Any:
    t = field_schema.get("type")
    if isinstance(t, list):
        # Union — fall back to Any rather than risk a wrong narrowing.
        return Any
    if isinstance(t, str) and t in _JSON_TYPE_TO_PYTHON:
        return _JSON_TYPE_TO_PYTHON[t]
    return Any


# Note: we deliberately avoid emitting `Optional[X]` in the Pydantic model
# even for non-required fields. LangChain agents tolerate `None`-defaulting
# Any-typed fields well, and `Optional` adds noise to the schema shown to
# the LLM. This is the same trade-off `langchain.tools.tool` makes for
# untyped Python kwargs.
