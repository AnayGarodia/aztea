# SPDX-License-Identifier: Apache-2.0
"""Tests for aztea_anthropic — load_aztea_tools + execute_tool_use.

All HTTP via respx. We assert:
  - Catalog fetch shape: GET, Authorization, params, base_url.
  - Returned tools have exactly Anthropic's {name, description, input_schema}.
  - Filters drop the right agents.
  - execute_tool_use POSTs to the right per-agent endpoint with the right body.
  - execute_tool_use accepts both a raw dict AND an attribute-style object.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import respx
from aztea_anthropic import (
    execute_tool_use,
    execute_tool_use_async,
    load_aztea_tools,
    load_aztea_tools_async,
)


_CATALOG_PAYLOAD = {
    "agents": [
        {
            "agent_id": "agent-1",
            "slug": "cve-lookup",
            "name": "CVE Lookup",
            "description": "Look up a CVE by ID.",
            "price_per_call_usd": 0.03,
            "trust_score": 0.92,
            "input_schema": {
                "type": "object",
                "properties": {"cve_id": {"type": "string"}},
                "required": ["cve_id"],
            },
        },
        {
            "agent_id": "agent-2",
            "slug": "premium-scanner",
            "name": "Premium Scanner",
            "description": "Costly.",
            "price_per_call_usd": 0.50,
            "trust_score": 0.95,
            "input_schema": {"type": "object", "properties": {}},
        },
    ],
}


@respx.mock
def test_load_returns_anthropic_tool_shape():
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    tools = load_aztea_tools(api_key="az_test", base_url="https://aztea.test")
    assert len(tools) == 2
    # Anthropic expects EXACTLY these three keys.
    for tool in tools:
        assert set(tool.keys()) == {"name", "description", "input_schema"}
    cve = next(t for t in tools if t["name"] == "cve-lookup")
    assert "CVE" in cve["description"]
    assert cve["input_schema"]["required"] == ["cve_id"]


@respx.mock
def test_load_sends_bearer_token():
    route = respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    load_aztea_tools(api_key="az_my_secret", base_url="https://aztea.test")
    assert route.calls.last.request.headers.get("authorization") == "Bearer az_my_secret"


@respx.mock
def test_load_filters_by_price():
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    tools = load_aztea_tools(
        api_key="az_test", base_url="https://aztea.test", max_price_usd=0.10,
    )
    names = {t["name"] for t in tools}
    assert "premium-scanner" not in names
    assert "cve-lookup" in names


@respx.mock
def test_load_filters_by_trust():
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    tools = load_aztea_tools(
        api_key="az_test", base_url="https://aztea.test", min_trust=0.94,
    )
    names = {t["name"] for t in tools}
    assert "cve-lookup" not in names  # 0.92 < 0.94
    assert "premium-scanner" in names  # 0.95 >= 0.94


@respx.mock
def test_execute_tool_use_with_dict():
    route = respx.post("https://aztea.test/registry/agents/cve-lookup/call").mock(
        return_value=httpx.Response(
            200, json={"job_id": "job_1", "status": "complete",
                       "output": {"cvss": 9.8}},
        ),
    )
    result = execute_tool_use(
        {"name": "cve-lookup", "input": {"cve_id": "CVE-2021-44228"}},
        api_key="az_test",
        base_url="https://aztea.test",
    )
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["input_payload"]["cve_id"] == "CVE-2021-44228"
    assert result["output"]["cvss"] == 9.8


@respx.mock
def test_execute_tool_use_with_attr_object():
    """Anthropic SDK returns ToolUseBlock with .name and .input attributes —
    our helper must accept that without forcing callers to import the SDK."""
    route = respx.post("https://aztea.test/registry/agents/cve-lookup/call").mock(
        return_value=httpx.Response(200, json={"output": {"ok": True}}),
    )
    block = SimpleNamespace(
        type="tool_use",
        id="toolu_abc",
        name="cve-lookup",
        input={"cve_id": "CVE-2021-44228"},
    )
    execute_tool_use(block, api_key="az_test", base_url="https://aztea.test")
    assert route.called


@respx.mock
def test_execute_tool_use_rejects_missing_name():
    with pytest.raises(ValueError):
        execute_tool_use(
            {"input": {"x": 1}},  # no name
            api_key="az_test",
            base_url="https://aztea.test",
        )


# ─── Async surface ─────────────────────────────────────────────────────────


@respx.mock
def test_async_load_and_execute():
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    route = respx.post("https://aztea.test/registry/agents/cve-lookup/call").mock(
        return_value=httpx.Response(200, json={"output": {"ok": True}}),
    )

    async def _run():
        tools = await load_aztea_tools_async(
            api_key="az_test", base_url="https://aztea.test",
        )
        assert tools
        result = await execute_tool_use_async(
            {"name": "cve-lookup", "input": {"cve_id": "x"}},
            api_key="az_test",
            base_url="https://aztea.test",
        )
        return result

    out = asyncio.run(_run())
    assert out["output"]["ok"] is True
    assert route.called


# ─── /review 2026-05-27 regression guards ──────────────────────────────────


@respx.mock
def test_anthropic_tool_name_preserves_kebab_case_slug():
    """Anthropic tool-name spec allows ^[a-zA-Z0-9_-]{1,128}$ — including
    hyphens. The previous version replaced `-` with `_`, which broke
    execute_tool_use because the backend's canonical slug is kebab-case
    and never recognised the underscored form. Every real Claude→Aztea
    call would 404. This test pins the canonical-slug-as-tool-name
    contract."""
    respx.get("https://aztea.test/registry/agents").mock(
        return_value=httpx.Response(200, json=_CATALOG_PAYLOAD),
    )
    tools = load_aztea_tools(api_key="az_test", base_url="https://aztea.test")
    names = {t["name"] for t in tools}
    assert names == {"cve-lookup", "premium-scanner"}, (
        f"Anthropic tools must use kebab-case slugs verbatim, got: {names!r}"
    )


@respx.mock
def test_execute_tool_use_posts_to_canonical_kebab_slug():
    """The end-to-end version of the slug-preservation test: when Claude
    returns a tool_use block with name='cve-lookup', the POST must hit
    /registry/agents/cve-lookup/call, not /registry/agents/cve_lookup/call."""
    route = respx.post("https://aztea.test/registry/agents/cve-lookup/call").mock(
        return_value=httpx.Response(200, json={"output": {"ok": True}}),
    )
    execute_tool_use(
        {"name": "cve-lookup", "input": {"cve_id": "CVE-X"}},
        api_key="az_test", base_url="https://aztea.test",
    )
    assert route.called


@respx.mock
def test_load_filters_by_tag_kwarg():
    """The `tag` kwarg (renamed from `category`) must forward as a `tag=`
    query param. Pre-fix, `category=` silently no-op'd because the backend
    has no such param — users thought they'd filtered to security agents
    but got the whole catalog."""
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
