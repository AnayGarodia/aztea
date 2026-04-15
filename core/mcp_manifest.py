"""
mcp_manifest.py — Build MCP-compatible tool manifests from registry agents.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_MCP_TOOL_PREFIX = "agentmarket__"
_DEFAULT_SCHEMA = {"type": "object", "additionalProperties": True}
_FIELD_TYPE_MAP = {
    "str": "string",
    "string": "string",
    "text": "string",
    "textarea": "string",
    "select": "string",
    "enum": "string",
    "int": "integer",
    "integer": "integer",
    "number": "number",
    "float": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "array": "array",
    "list": "array",
    "object": "object",
    "dict": "object",
}


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _field_json_schema(field: dict[str, Any]) -> dict[str, Any]:
    raw_type = str(field.get("type") or field.get("input_type") or "string").strip().lower()
    json_type = _FIELD_TYPE_MAP.get(raw_type, "string")
    schema: dict[str, Any] = {"type": json_type}

    description = str(field.get("description") or field.get("hint") or "").strip()
    if description:
        schema["description"] = description

    options = field.get("options")
    if isinstance(options, list):
        enum_values = [str(option).strip() for option in options if str(option).strip()]
        if enum_values and json_type == "string":
            schema["enum"] = enum_values

    default_value = field.get("default")
    if default_value is not None:
        schema["default"] = default_value
    return schema


def _fields_to_json_schema(fields: list[Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or field.get("key") or "").strip()
        if not name:
            continue
        properties[name] = _field_json_schema(field)
        if bool(field.get("required")):
            required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def normalize_schema(raw_schema: Any) -> dict[str, Any]:
    if not isinstance(raw_schema, dict) or not raw_schema:
        return dict(_DEFAULT_SCHEMA)

    schema = dict(raw_schema)
    fields = schema.get("fields")
    if isinstance(fields, list) and not any(
        key in schema for key in ("type", "properties", "items", "oneOf", "allOf", "anyOf")
    ):
        converted = _fields_to_json_schema(fields)
        if converted.get("properties"):
            return converted
        return dict(_DEFAULT_SCHEMA)

    if "properties" in schema and "type" not in schema:
        schema["type"] = "object"
    if schema.get("type") == "object" and "additionalProperties" not in schema:
        schema["additionalProperties"] = True
    return schema


def _tool_name(agent: dict[str, Any], used_names: set[str]) -> str:
    agent_id = str(agent.get("agent_id") or "").strip().replace("-", "")
    suffix = agent_id[:8] or "agent"
    base_name = _slugify(str(agent.get("name") or ""))
    if not base_name:
        base_name = f"tool_{suffix}"

    candidate = f"{_MCP_TOOL_PREFIX}{base_name}"
    if candidate in used_names:
        candidate = f"{candidate}_{suffix}"
    while candidate in used_names:
        candidate = f"{candidate}_x"
    used_names.add(candidate)
    return candidate


def build_mcp_tool_entries(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for agent in agents:
        agent_id = str(agent.get("agent_id") or "").strip()
        if not agent_id:
            continue
        name = str(agent.get("name") or "").strip() or f"Agent {agent_id[:8]}"
        description = str(agent.get("description") or "").strip()
        tool_description = f"{name}: {description}" if description else name
        tool_name = _tool_name(agent, used_names)
        input_schema = normalize_schema(agent.get("input_schema"))
        output_schema = normalize_schema(agent.get("output_schema"))
        tool = {
            "name": tool_name,
            "description": tool_description,
            "inputSchema": input_schema,
            "outputSchema": output_schema,
        }
        entries.append({"agent_id": agent_id, "tool_name": tool_name, "tool": tool})
    return entries


def build_mcp_manifest(agents: list[dict[str, Any]]) -> dict[str, Any]:
    entries = build_mcp_tool_entries(agents)
    tools = [entry["tool"] for entry in entries]
    return {
        "tools": tools,
        "count": len(tools),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

