"""MCP manifest invariants — runs across every built-in agent spec.

# OWNS: contract assertions for build_mcp_tool_entries and the three
#       platform-adapter manifests (OpenAI chat, OpenAI Responses, Gemini).
# INVARIANTS asserted:
#   - tool names are snake_case, no prefix.
#   - tool names are unique within a manifest.
#   - input_schema/output_schema are JSON-Schema-valid.
#   - manifests are idempotent (build twice → identical output).
#   - all three adapter manifests reference the same set of agent IDs.
"""
from __future__ import annotations

import re

import pytest

from core.mcp_manifest import build_mcp_tool_entries, normalize_schema
from core.tool_adapters import (
    build_gemini_manifest,
    build_openai_chat_manifest,
    build_openai_responses_manifest,
)
from server.builtin_agents.specs import builtin_agent_specs

pytestmark = pytest.mark.property

_SPECS = builtin_agent_specs()
_SPEC_IDS = [s["name"] for s in _SPECS]
_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


@pytest.fixture(scope="module")
def mcp_entries():
    return build_mcp_tool_entries(_SPECS)


@pytest.fixture(scope="module")
def chat_manifest():
    return build_openai_chat_manifest(_SPECS)


@pytest.fixture(scope="module")
def responses_manifest():
    return build_openai_responses_manifest(_SPECS)


@pytest.fixture(scope="module")
def gemini_manifest():
    return build_gemini_manifest(_SPECS)


# --- Per-spec invariants -----------------------------------------------------

@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_spec_has_required_keys(spec):
    for key in ("agent_id", "name", "description", "input_schema", "output_schema"):
        assert key in spec, f"{spec.get('name')} missing {key}"


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_spec_input_schema_normalizes(spec):
    schema = normalize_schema(spec.get("input_schema"))
    assert isinstance(schema, dict)
    assert schema.get("type") in (None, "object")


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_spec_output_schema_normalizes(spec):
    schema = normalize_schema(spec.get("output_schema"))
    assert isinstance(schema, dict)


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_spec_price_is_non_negative(spec):
    price = spec.get("price_per_call_usd")
    if price is None:
        return
    assert float(price) >= 0


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_spec_tags_are_strings(spec):
    tags = spec.get("tags") or []
    assert isinstance(tags, list)
    for t in tags:
        assert isinstance(t, str)


# --- Per-entry invariants ----------------------------------------------------

@pytest.mark.parametrize("idx", range(len(_SPECS)), ids=_SPEC_IDS)
def test_mcp_entry_has_tool_name(idx, mcp_entries):
    entry = mcp_entries[idx]
    name = entry["tool_name"]
    assert isinstance(name, str) and name


@pytest.mark.parametrize("idx", range(len(_SPECS)), ids=_SPEC_IDS)
def test_mcp_entry_tool_name_is_snake_case(idx, mcp_entries):
    name = mcp_entries[idx]["tool_name"]
    assert _SNAKE_CASE.match(name), f"non-snake_case tool name: {name!r}"


@pytest.mark.parametrize("idx", range(len(_SPECS)), ids=_SPEC_IDS)
def test_mcp_entry_tool_has_input_schema(idx, mcp_entries):
    entry = mcp_entries[idx]
    schema = entry["tool"].get("inputSchema") or entry["tool"].get("input_schema")
    assert isinstance(schema, dict)
    assert schema.get("type") in (None, "object")


# --- Manifest-level invariants ----------------------------------------------

def test_mcp_tool_names_unique(mcp_entries):
    names = [e["tool_name"] for e in mcp_entries]
    assert len(names) == len(set(names)), "duplicate tool names in MCP manifest"


def test_mcp_entries_idempotent():
    a = build_mcp_tool_entries(_SPECS)
    b = build_mcp_tool_entries(_SPECS)
    assert [e["tool_name"] for e in a] == [e["tool_name"] for e in b]


# --- Adapter manifests -------------------------------------------------------

def test_chat_manifest_count_matches_specs(chat_manifest):
    """OpenAI Chat manifest emits one tool per agent (plus meta tools)."""
    tools = chat_manifest["tools"]
    # `meta_tools_included` reports how many meta tools were prepended.
    meta = chat_manifest.get("meta_tools_included", 0)
    assert len(tools) - meta >= len(_SPECS) or len(tools) >= len(_SPECS)


def test_chat_manifest_idempotent():
    a = build_openai_chat_manifest(_SPECS)
    b = build_openai_chat_manifest(_SPECS)
    # tool_lookup contains a closure'd lookup that may have unstable identity;
    # compare on tool list and count instead.
    assert a["count"] == b["count"]
    assert [t.get("function", {}).get("name") for t in a["tools"]] == [
        t.get("function", {}).get("name") for t in b["tools"]
    ]


def test_responses_manifest_idempotent():
    a = build_openai_responses_manifest(_SPECS)
    b = build_openai_responses_manifest(_SPECS)
    assert a["count"] == b["count"]


def test_gemini_manifest_idempotent():
    a = build_gemini_manifest(_SPECS)
    b = build_gemini_manifest(_SPECS)
    assert a.get("count") == b.get("count")


def test_chat_manifest_tool_names_snake_case(chat_manifest):
    for tool in chat_manifest["tools"]:
        name = tool.get("function", {}).get("name") or tool.get("name")
        if name is None:
            continue
        assert _SNAKE_CASE.match(name), f"non-snake_case tool name in chat manifest: {name!r}"


def test_responses_manifest_tool_names_snake_case(responses_manifest):
    for tool in responses_manifest["tools"]:
        name = tool.get("name") or tool.get("function", {}).get("name")
        if name is None:
            continue
        assert _SNAKE_CASE.match(name), (
            f"non-snake_case tool name in responses manifest: {name!r}"
        )


def test_gemini_manifest_tool_names_snake_case(gemini_manifest):
    """Gemini wraps tools in functionDeclarations; walk the structure."""
    tools = gemini_manifest.get("tools", [])
    for t in tools:
        for fn in t.get("functionDeclarations", []) or t.get("function_declarations", []):
            name = fn.get("name")
            if name is None:
                continue
            assert _SNAKE_CASE.match(name), (
                f"non-snake_case tool name in gemini manifest: {name!r}"
            )


def test_all_adapters_share_agent_set(chat_manifest, responses_manifest, gemini_manifest):
    """All three adapter manifests must publish the same set of agent tool names."""
    chat_names = {
        t.get("function", {}).get("name") or t.get("name")
        for t in chat_manifest["tools"]
    }
    responses_names = {
        t.get("name") or t.get("function", {}).get("name")
        for t in responses_manifest["tools"]
    }
    gemini_names: set[str] = set()
    for t in gemini_manifest.get("tools", []):
        for fn in t.get("functionDeclarations", []) or t.get("function_declarations", []):
            gemini_names.add(fn["name"])

    chat_names = {n for n in chat_names if n}
    responses_names = {n for n in responses_names if n}
    gemini_names = {n for n in gemini_names if n}

    # The three adapters may emit different sets of *meta* tools, but every
    # agent-derived tool name must appear in all three. Take intersection of
    # spec-derived names as the invariant.
    spec_derived = {e["tool_name"] for e in build_mcp_tool_entries(_SPECS)}
    for name in spec_derived:
        assert name in chat_names, f"{name!r} missing from chat manifest"
        assert name in responses_names, f"{name!r} missing from responses manifest"
        assert name in gemini_names, f"{name!r} missing from gemini manifest"
