# SPDX-License-Identifier: Apache-2.0
"""Snapshot tests for core.tool_adapters.build_anthropic_manifest.

# OWNS: shape regression for the Anthropic Messages tool manifest builder.
#       Added 2026-05-26 alongside the new `aztea-anthropic` PyPI package.
# INVARIANTS:
#   - Every emitted tool has exactly the keys Anthropic expects:
#     {name, description, input_schema}. No extra keys; no OpenAI-style
#     "function": {...} envelope; no Gemini-style "parameters" rename.
#   - The lookup table is structurally identical to the OpenAI / Gemini
#     builders so the server-side dispatch code can share one helper.
"""

from __future__ import annotations

from core.tool_adapters import build_anthropic_manifest


def _agent(slug: str, **extras):
    base = {
        "agent_id": f"agent_{slug.replace('-', '_')}",
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "description": f"A test agent named {slug}.",
        "input_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        "price_per_call_usd": 0.05,
        "trust_score": 0.9,
    }
    base.update(extras)
    return base


def _find_by_name_prefix(tools: list[dict], prefix: str) -> dict:
    """Find the registry tool whose name matches the agent slug (post-_catalog_entries
    naming). The manifest also includes meta_tools; we filter to the agent under test."""
    for tool in tools:
        if tool["name"].startswith(prefix.replace("-", "_")):
            return tool
    raise AssertionError(
        f"No tool with prefix {prefix!r} in: {[t['name'] for t in tools]}"
    )


def test_manifest_top_level_keys():
    out = build_anthropic_manifest([_agent("scan-secrets"), _agent("cve-lookup")])
    assert set(out.keys()) == {
        "tools", "count", "tool_format", "meta_tools_included", "tool_lookup",
    }
    assert out["tool_format"] == "anthropic_tools"
    assert out["count"] == len(out["tools"])


def test_every_tool_has_exactly_anthropic_keys():
    """Anthropic's Messages API rejects extra top-level keys on tool dicts.
    A drift here would cause silent 400s in customer integrations."""
    out = build_anthropic_manifest([_agent("scan-secrets"), _agent("cve-lookup")])
    for tool in out["tools"]:
        assert set(tool.keys()) == {"name", "description", "input_schema"}, (
            f"Anthropic tool has wrong keys: {set(tool.keys())!r}; "
            "expected exactly {name, description, input_schema}"
        )


def test_input_schema_preserves_types_and_required():
    """The schema flows through mcp_manifest.build_mcp_tool_entries which
    enriches each property with a `description` and may add
    `additionalProperties`. We don't pin those — they're not load-bearing
    for Anthropic — but the type + required must survive verbatim, or
    callers' tool inputs will get rejected by the Aztea backend."""
    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "depth": {"type": "integer"},
        },
        "required": ["url"],
    }
    out = build_anthropic_manifest([_agent("crawler", input_schema=schema)])
    crawler_tool = _find_by_name_prefix(out["tools"], "crawler")
    actual = crawler_tool["input_schema"]
    assert actual["type"] == "object"
    assert actual["properties"]["url"]["type"] == "string"
    assert actual["properties"]["depth"]["type"] == "integer"
    assert actual.get("required") == ["url"]


def test_empty_catalog_returns_only_meta_tools():
    """_catalog_entries always prepends meta_tools (manage_job, etc.) so an
    empty agent catalog still yields a non-empty manifest. Anthropic-side
    consumers always get the platform's control-plane tools alongside any
    registry agents."""
    out = build_anthropic_manifest([])
    # meta_tools_included is set by the builder; assert it surfaces in the
    # envelope so downstream consumers know what they got.
    assert out["meta_tools_included"] is True
    # The manifest is not empty — it has the meta tools the platform always
    # ships. We don't pin the exact count (meta_tools changes shape over
    # time); we just assert the envelope shape stays consistent.
    assert out["count"] == len(out["tools"])
    assert out["count"] >= 1  # at least one meta tool is shipped


def test_tool_lookup_indexes_by_emitted_name_for_registry_agents():
    out = build_anthropic_manifest([_agent("dependency-auditor")])
    registry_tool = _find_by_name_prefix(out["tools"], "dependency-auditor")
    name = registry_tool["name"]
    assert name in out["tool_lookup"]
    assert "agent_id" in out["tool_lookup"][name]
    # Registry agents have agent_id; meta tools have None.
    assert out["tool_lookup"][name]["agent_id"] is not None
    assert out["tool_lookup"][name]["agent_id"].startswith("agent_")
    assert out["tool_lookup"][name]["kind"] == "registry_agent"


def test_meta_tools_lookup_has_kind_meta_tool():
    out = build_anthropic_manifest([])
    # Pick any meta tool — confirm its lookup entry tags it as meta.
    first_name = out["tools"][0]["name"]
    assert out["tool_lookup"][first_name]["kind"] == "meta_tool"
    assert out["tool_lookup"][first_name]["agent_id"] is None
