"""Tool-manifest builders for the three supported LLM tool-call formats.

This module converts the Aztea agent registry into the tool-definition shapes
expected by different LLM APIs. All three builders start from the same internal
catalog (``_catalog_entries``) and produce a format-specific structure.

Formats supported
-----------------
``build_openai_chat_manifest``
    OpenAI Chat Completions ``tools`` array  (``type: "function"`` wrappers).
    Includes full Aztea metadata (price, trust score, privacy flags) in the
    ``function.metadata`` field.

``build_openai_responses_manifest``
    OpenAI Responses API  ``tools`` array  (``type: "function"`` flat objects,
    ``strict: False``).  Lighter metadata — only trust_score_by_client included.

``build_gemini_manifest``
    Gemini ``functionDeclarations`` format, wrapped in a single
    ``{"tools": [{"functionDeclarations": [...]}]}`` envelope.

All three builders embed both registry agents and meta-tools (from
``scripts/aztea_mcp_meta_tools.py``) in the catalog. Meta-tools are platform
utilities (e.g. wallet check, job status) that aren't backed by a registry entry.

Each returned dict also includes a ``tool_lookup`` mapping ``name → metadata``
so callers can resolve a tool-call name back to ``agent_id`` and privacy flags
without rescanning the tool list.
"""

from __future__ import annotations

from typing import Any

from core import mcp_manifest
from scripts import aztea_mcp_meta_tools as meta_tools


def _catalog_entries(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    registry_entries = mcp_manifest.build_mcp_tool_entries(agents)
    catalog: list[dict[str, Any]] = []

    for tool in meta_tools.get_meta_tools():
        catalog.append(
            {
                "name": str(tool.get("name") or "").strip(),
                "description": str(tool.get("description") or "").strip(),
                "input_schema": tool.get("input_schema") or {"type": "object", "properties": {}, "required": []},
                "kind": "meta_tool",
                "agent_id": None,
                "agent": None,
            }
        )

    for entry in registry_entries:
        tool = entry.get("tool") or {}
        agent_id = str(entry.get("agent_id") or "").strip() or None
        agent = next(
            (item for item in agents if str(item.get("agent_id") or "").strip() == agent_id),
            None,
        )
        catalog.append(
            {
                "name": str(tool.get("name") or "").strip(),
                "description": str(tool.get("description") or "").strip(),
                "input_schema": tool.get("input_schema") or {"type": "object", "additionalProperties": True},
                "kind": "registry_agent",
                "agent_id": agent_id,
                "agent": agent,
            }
        )

    return [item for item in catalog if item.get("name")]


def build_openai_chat_manifest(agents: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an OpenAI Chat Completions ``tools`` array from the agent registry.

    Each tool is a ``{type: "function", function: {name, description, parameters,
    metadata}}`` object. The ``metadata`` field carries Aztea-specific data
    (price, trust score, privacy flags) that OpenAI passes through opaquely.

    Returns ``{tools, count, tool_format, meta_tools_included, tool_lookup}``.
    """
    tools: list[dict[str, Any]] = []
    tool_lookup: dict[str, dict[str, Any]] = {}
    for item in _catalog_entries(agents):
        metadata: dict[str, Any] = {"aztea_tool_kind": item["kind"]}
        if item["agent_id"]:
            metadata["aztea_agent_id"] = item["agent_id"]
        agent = item.get("agent") or {}
        if agent:
            metadata["price_per_call_usd"] = float(agent.get("price_per_call_usd") or 0)
            metadata["trust_score"] = agent.get("trust_score")
            metadata["success_rate"] = agent.get("success_rate")
            metadata["trust_score_by_client"] = agent.get("by_client") or {}
            metadata["privacy"] = {
                "pii_safe": bool(agent.get("pii_safe")),
                "outputs_not_stored": bool(agent.get("outputs_not_stored")),
                "audit_logged": bool(agent.get("audit_logged")),
                "region_locked": agent.get("region_locked"),
            }
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"],
                    "parameters": item["input_schema"],
                    "metadata": metadata,
                },
            }
        )
        tool_lookup[item["name"]] = {
            "kind": item["kind"],
            "agent_id": item["agent_id"],
            "privacy": {
                "pii_safe": bool(agent.get("pii_safe")) if agent else False,
                "outputs_not_stored": bool(agent.get("outputs_not_stored")) if agent else False,
                "audit_logged": bool(agent.get("audit_logged")) if agent else False,
                "region_locked": agent.get("region_locked") if agent else None,
            },
        }
    return {
        "tools": tools,
        "count": len(tools),
        "tool_format": "openai_chat_completions",
        "meta_tools_included": True,
        "tool_lookup": tool_lookup,
    }


def build_openai_responses_manifest(agents: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an OpenAI Responses API ``tools`` array (flat function objects).

    Uses the ``{type: "function", name, description, parameters, strict: False}``
    shape required by the Responses API (different from Chat Completions).
    Returns ``{tools, count, tool_format, meta_tools_included, tool_lookup}``.
    """
    tools: list[dict[str, Any]] = []
    tool_lookup: dict[str, dict[str, Any]] = {}
    for item in _catalog_entries(agents):
        tools.append(
            {
                "type": "function",
                "name": item["name"],
                "description": item["description"],
                "parameters": item["input_schema"],
                "strict": False,
            }
        )
        tool_lookup[item["name"]] = {
            "kind": item["kind"],
            "agent_id": item["agent_id"],
            "trust_score_by_client": (item.get("agent") or {}).get("by_client") or {},
        }
    return {
        "tools": tools,
        "count": len(tools),
        "tool_format": "openai_responses_function",
        "meta_tools_included": True,
        "tool_lookup": tool_lookup,
    }


def build_gemini_manifest(agents: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Gemini ``functionDeclarations`` tool list.

    Returns the canonical Gemini shape:
    ``{tools: [{functionDeclarations: [...]}], function_declarations,
    count, tool_format, meta_tools_included, tool_lookup}``.
    The top-level ``function_declarations`` key is a convenience copy for
    callers that need the flat list without unwrapping the outer envelope.
    """
    declarations: list[dict[str, Any]] = []
    tool_lookup: dict[str, dict[str, Any]] = {}
    for item in _catalog_entries(agents):
        declarations.append(
            {
                "name": item["name"],
                "description": item["description"],
                "parameters": item["input_schema"],
            }
        )
        tool_lookup[item["name"]] = {
            "kind": item["kind"],
            "agent_id": item["agent_id"],
            "trust_score_by_client": (item.get("agent") or {}).get("by_client") or {},
        }
    return {
        "tools": [{"functionDeclarations": declarations}],
        "function_declarations": declarations,
        "count": len(declarations),
        "tool_format": "gemini_function_declarations",
        "meta_tools_included": True,
        "tool_lookup": tool_lookup,
    }
