"""Tool factory — Aztea catalog → Anthropic Messages tool dicts.

# OWNS: load_aztea_tools (sync + async), execute_tool_use (sync + async).
#       The manifest shape is mirrored in core.tool_adapters.
#       build_anthropic_manifest so a server-side endpoint can hand the
#       same dict over HTTP without re-implementing.
# INVARIANTS:
#   - Every returned tool dict matches Anthropic's expected shape exactly:
#     keys are {name, description, input_schema}. No extra keys, no
#     OpenAI-style "function": {...} envelope.
#   - execute_tool_use(block) accepts EITHER a raw anthropic.types.ToolUseBlock
#     OR a plain dict with `name` + `input` keys — same shape both yield.
#     Lets callers compose without importing anthropic just to type-check.
#   - Filter kwargs (category, max_price_usd, min_trust) match the
#     LangChain adapter signature so a polyglot team uses one mental model.
"""

from __future__ import annotations

from typing import Any

import httpx

__all__ = [
    "load_aztea_tools",
    "load_aztea_tools_async",
    "execute_tool_use",
    "execute_tool_use_async",
]


_DEFAULT_BASE_URL = "https://aztea.ai"
_DEFAULT_TIMEOUT_SECONDS = 30.0


# ─── Catalog loading ───────────────────────────────────────────────────────


def load_aztea_tools(
    *,
    api_key: str,
    base_url: str | None = None,
    tag: str | None = None,
    max_price_usd: float | None = None,
    min_trust: float | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    include_reputation: bool = True,
) -> list[dict[str, Any]]:
    """Fetch the Aztea catalog and return Anthropic-Messages-shaped tool dicts.

    The returned list has the exact shape Anthropic's `messages.create(tools=...)`
    expects: `[{"name": "...", "description": "...", "input_schema": {...}}, ...]`.
    Pair with `execute_tool_use(block, api_key=...)` to run any Aztea agent
    Claude picks.

    Args:
        api_key: Aztea API key. Generate at https://aztea.ai/account/keys.
        base_url: Override the API base URL (default: https://aztea.ai).
        tag: Filter to agents tagged with this string (e.g. "security",
            "data"). Maps to the backend `/registry/agents?tag=` query
            param. (An earlier version accepted `category=`, which silently
            no-op'd because the backend doesn't expose that param. /review
            caught this 2026-05-27.)
        max_price_usd: Drop agents whose per-call price exceeds this.
        min_trust: Drop agents whose trust score is below this (0.0–1.0).
        timeout: HTTP timeout in seconds.
        include_reputation: Pass-through to the catalog endpoint.

    Returns: list of Anthropic-tool-shaped dicts.
    """
    resolved_base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    agents = _fetch_catalog_sync(
        api_key=api_key,
        base_url=resolved_base,
        tag=tag,
        timeout=timeout,
        include_reputation=include_reputation,
    )
    filtered = _filter_agents(
        agents, max_price_usd=max_price_usd, min_trust=min_trust,
    )
    return [_to_anthropic_tool(agent) for agent in filtered]


async def load_aztea_tools_async(
    *,
    api_key: str,
    base_url: str | None = None,
    tag: str | None = None,
    max_price_usd: float | None = None,
    min_trust: float | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    include_reputation: bool = True,
) -> list[dict[str, Any]]:
    """Async variant of `load_aztea_tools` — same signature, same return shape."""
    resolved_base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    agents = await _fetch_catalog_async(
        api_key=api_key,
        base_url=resolved_base,
        tag=tag,
        timeout=timeout,
        include_reputation=include_reputation,
    )
    filtered = _filter_agents(
        agents, max_price_usd=max_price_usd, min_trust=min_trust,
    )
    return [_to_anthropic_tool(agent) for agent in filtered]


# ─── Tool-use execution ────────────────────────────────────────────────────


def execute_tool_use(
    tool_use: Any,
    *,
    api_key: str,
    base_url: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute an Anthropic `tool_use` block via the Aztea backend.

    Accepts EITHER an `anthropic.types.ToolUseBlock` OR a plain dict with
    `name` (slug) and `input` (dict) keys. Returns the backend response
    verbatim — typically `{"job_id", "status", "output", "latency_ms", ...}`.

    To pass the result back to Claude:

        followup = client.messages.create(
            model=..., max_tokens=1024,
            tools=tools,
            messages=[..., {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": str(result["output"]),
                }],
            }],
        )
    """
    name, input_payload = _extract_name_and_input(tool_use)
    resolved_base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{resolved_base}/registry/agents/{name}/call",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"input_payload": input_payload},
        )
    resp.raise_for_status()
    return resp.json()


async def execute_tool_use_async(
    tool_use: Any,
    *,
    api_key: str,
    base_url: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Async variant of `execute_tool_use`."""
    name, input_payload = _extract_name_and_input(tool_use)
    resolved_base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{resolved_base}/registry/agents/{name}/call",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"input_payload": input_payload},
        )
    resp.raise_for_status()
    return resp.json()


# ─── Helpers ───────────────────────────────────────────────────────────────


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
    return resp.json().get("agents") or []


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
    return resp.json().get("agents") or []


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
            if trust < min_trust:
                continue
        out.append(agent)
    return out


def _to_anthropic_tool(agent: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages API tool shape: {name, description, input_schema}."""
    slug = str(agent.get("slug") or agent.get("agent_id") or "").strip()
    if not slug:
        raise ValueError(f"Agent missing slug + agent_id: {agent!r}")
    # Anthropic tool names must match ^[a-zA-Z0-9_-]{1,128}$ — hyphens are
    # ALLOWED, so we use the canonical kebab-case slug verbatim. (An earlier
    # version of this adapter converted `-` to `_`, mirroring the LangChain
    # adapter where StructuredTool.name must be a valid Python identifier.
    # That copy-paste was wrong for Anthropic: the underscored name didn't
    # match the backend's canonical slug, so every execute_tool_use call
    # returned 404. /review caught this 2026-05-27.)
    return {
        "name": slug,
        "description": str(
            agent.get("description") or agent.get("summary") or f"Aztea agent {slug}",
        ),
        "input_schema": agent.get("input_schema") or {
            "type": "object", "properties": {},
        },
    }


def _extract_name_and_input(tool_use: Any) -> tuple[str, dict[str, Any]]:
    """Accept either an SDK ToolUseBlock or a plain dict; return (name, input)."""
    # SDK object path — duck-type to avoid importing anthropic in this helper.
    name = getattr(tool_use, "name", None)
    input_payload = getattr(tool_use, "input", None)
    if name is None and isinstance(tool_use, dict):
        name = tool_use.get("name")
        input_payload = tool_use.get("input")
    if not name:
        raise ValueError(
            f"tool_use is missing a `name` field: {tool_use!r}"
        )
    if not isinstance(input_payload, dict):
        input_payload = {}
    return str(name), input_payload
