# SPDX-License-Identifier: Apache-2.0
"""Tests for the aztea_langchain.load_aztea_tools factory.

All HTTP is mocked via respx — no live Aztea backend needed. We assert:
  - Catalog fetch makes the right GET (auth header, params, base_url).
  - One StructuredTool per agent, with name + description populated.
  - Filter kwargs drop the right agents.
  - Tool .invoke() makes the right POST and returns the backend body.
  - Async variant has the same surface and works under asyncio.run.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from aztea_langchain import load_aztea_tools, load_aztea_tools_async


_CATALOG_PAYLOAD = {
    "agents": [
        {
            "agent_id": "agent-1",
            "slug": "cve-lookup",
            "name": "CVE Lookup",
            "description": "Look up a CVE by ID and return its CVSS + affected packages.",
            "price_per_call_usd": 0.03,
            "trust_score": 0.92,
            "input_schema": {
                "type": "object",
                "properties": {
                    "cve_id": {"type": "string"},
                },
                "required": ["cve_id"],
            },
        },
        {
            "agent_id": "agent-2",
            "slug": "expensive-agent",
            "name": "Expensive Agent",
            "description": "Costs a lot.",
            "price_per_call_usd": 1.50,
            "trust_score": 0.50,
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "agent_id": "agent-3",
            "slug": "low-trust-agent",
            "name": "Low Trust Agent",
            "description": "Brand-new and unproven.",
            "price_per_call_usd": 0.01,
            "trust_score": 0.20,
            "input_schema": {"type": "object", "properties": {}},
        },
    ],
}


# ─── Sync surface ──────────────────────────────────────────────────────────


@respx.mock
def test_load_aztea_tools_returns_one_tool_per_agent():
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    tools = load_aztea_tools(api_key="az_test", base_url="https://aztea.test")
    assert len(tools) == 3
    names = {tool.name for tool in tools}
    assert names == {"cve_lookup", "expensive_agent", "low_trust_agent"}


@respx.mock
def test_load_aztea_tools_filters_by_price():
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    tools = load_aztea_tools(
        api_key="az_test", base_url="https://aztea.test", max_price_usd=0.10,
    )
    names = {tool.name for tool in tools}
    assert "expensive_agent" not in names  # $1.50 > $0.10
    assert "cve_lookup" in names           # $0.03 < $0.10


@respx.mock
def test_load_aztea_tools_filters_by_trust():
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    tools = load_aztea_tools(
        api_key="az_test", base_url="https://aztea.test", min_trust=0.80,
    )
    names = {tool.name for tool in tools}
    assert "low_trust_agent" not in names  # 0.20 < 0.80
    assert "cve_lookup" in names           # 0.92 >= 0.80


@respx.mock
def test_load_aztea_tools_sends_bearer_token():
    route = respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    load_aztea_tools(api_key="az_my_secret_key", base_url="https://aztea.test")
    assert route.called
    request = route.calls.last.request
    assert request.headers.get("authorization") == "Bearer az_my_secret_key"


@respx.mock
def test_tool_invoke_posts_to_per_agent_call_endpoint():
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    call_route = respx.post("https://aztea.test/registry/agents/agent-1/call").mock(
        return_value=httpx.Response(
            200,
            json={"job_id": "job_xyz", "status": "complete",
                  "output": {"cvss": 9.8, "affected": ["log4j"]}},
        ),
    )
    tools = load_aztea_tools(api_key="az_test", base_url="https://aztea.test")
    cve_tool = next(t for t in tools if t.name == "cve_lookup")
    result = cve_tool.invoke({"cve_id": "CVE-2021-44228"})
    assert call_route.called
    sent_body = json.loads(call_route.calls.last.request.content)
    assert sent_body["input_payload"]["cve_id"] == "CVE-2021-44228"
    assert result["output"]["cvss"] == 9.8


@respx.mock
def test_tool_invoke_rejects_invalid_input_via_pydantic():
    """The lazily-materialized Pydantic schema validates inputs locally so
    we don't waste an Aztea call charge on a malformed payload."""
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    tools = load_aztea_tools(api_key="az_test", base_url="https://aztea.test")
    cve_tool = next(t for t in tools if t.name == "cve_lookup")
    # `cve_id` is required per the input_schema — empty kwargs should reject
    # at the Pydantic layer before any HTTP call.
    with pytest.raises(Exception):
        cve_tool.invoke({})


# ─── Async surface ─────────────────────────────────────────────────────────


@respx.mock
def test_load_aztea_tools_async_returns_same_shape():
    import asyncio
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    tools = asyncio.run(
        load_aztea_tools_async(api_key="az_test", base_url="https://aztea.test"),
    )
    assert len(tools) == 3
    names = {tool.name for tool in tools}
    assert names == {"cve_lookup", "expensive_agent", "low_trust_agent"}


# ─── Edge cases ────────────────────────────────────────────────────────────


@respx.mock
def test_empty_catalog_returns_empty_tool_list():
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json={"agents": []}),
    )
    tools = load_aztea_tools(api_key="az_test", base_url="https://aztea.test")
    assert tools == []


@respx.mock
def test_4xx_from_catalog_raises():
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"}),
    )
    with pytest.raises(httpx.HTTPStatusError):
        load_aztea_tools(api_key="bad_key", base_url="https://aztea.test")


# ─── /review 2026-05-27 regression guards ──────────────────────────────────


@respx.mock
def test_load_filters_by_tag_kwarg():
    """The `tag` kwarg (renamed from `category`) must forward as a `tag=`
    query param. Pre-fix, `category=` silently no-op'd because the backend
    has no such param — users thought they'd filtered to security agents
    but got the whole catalog. /review caught this 2026-05-27."""
    route = respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    load_aztea_tools(
        api_key="az_test", base_url="https://aztea.test", tag="security",
    )
    sent_url = str(route.calls.last.request.url)
    assert "tag=security" in sent_url, (
        f"tag kwarg must forward as ?tag= query param; got URL: {sent_url}"
    )
