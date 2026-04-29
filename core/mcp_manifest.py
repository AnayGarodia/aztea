"""
mcp_manifest.py — Build MCP-compatible tool manifests from registry agents.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_MCP_TOOL_PREFIX = ""  # no prefix — tool names are plain snake_case agent name slugs
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
    """Sanitise a JSON schema dict for safe use in MCP tool manifests.

    Removes ``$ref``, ``definitions``, and other constructs that MCP clients
    may not handle. Returns ``_DEFAULT_SCHEMA`` for empty or non-dict inputs.
    """
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

    # snake_case, no prefix — identical on both HTTP and stdio surfaces
    candidate = base_name
    if candidate in used_names:
        candidate = f"{candidate}_{suffix}"
    while candidate in used_names:
        candidate = f"{candidate}_x"
    used_names.add(candidate)
    return candidate


def _quality_line(agent: dict[str, Any]) -> str:
    """Build a one-line quality + pricing summary from reputation fields."""
    parts: list[str] = []

    # Verification badge
    verified = agent.get("verified")
    if verified:
        parts.append("verified")

    # Reputation signals
    trust = agent.get("trust_score")
    if trust is not None:
        parts.append(f"trust {int(trust)}/100")
    success = agent.get("success_rate")
    if success is not None:
        parts.append(f"{int(round(float(success) * 100))}% success")
    latency = agent.get("avg_latency_ms")
    if latency is not None and float(latency) > 0:
        ms = float(latency)
        parts.append(f"~{ms/1000:.1f}s avg" if ms >= 1000 else f"~{int(ms)}ms avg")
    calls = agent.get("total_calls")
    if calls is not None and int(calls) > 0:
        parts.append(f"{int(calls):,} calls")
    by_client = agent.get("by_client")
    if isinstance(by_client, dict) and by_client:
        ranked = sorted(
            (
                (str(client_id), float(score))
                for client_id, score in by_client.items()
                if client_id and score is not None
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:3]
        if ranked:
            labels = ", ".join(f"{client_id} {int(score)}" for client_id, score in ranked)
            parts.append(f"client trust: {labels}")

    # Pricing
    price = agent.get("price_per_call_usd")
    pricing_model = str(agent.get("pricing_model") or "fixed").lower()
    if price is not None:
        if pricing_model == "per_unit":
            parts.append(f"${float(price):.3f}/unit (variable)")
        elif pricing_model == "tiered":
            parts.append(f"from ${float(price):.3f}/call (tiered)")
        else:
            parts.append(f"${float(price):.3f}/call")

    return " | ".join(parts)


def _privacy_line(agent: dict[str, Any]) -> str:
    flags: list[str] = []
    if agent.get("pii_safe"):
        flags.append("pii-safe")
    if agent.get("outputs_not_stored"):
        flags.append("outputs not stored")
    if agent.get("audit_logged"):
        flags.append("audit logged")
    region = str(agent.get("region_locked") or "").strip()
    if region:
        flags.append(f"region {region}")
    return " | ".join(flags)


def _catalog_line(agent: dict[str, Any]) -> str:
    parts: list[str] = []
    category = str(agent.get("category") or "").strip()
    if category:
        parts.append(category)
    tooling_kind = str(agent.get("tooling_kind") or "").strip().replace("_", " ")
    if tooling_kind:
        parts.append(tooling_kind)
    stability_tier = str(agent.get("stability_tier") or "").strip()
    if stability_tier:
        parts.append(stability_tier)
    if agent.get("codex_recommended"):
        parts.append("Claude-ready")
    return " | ".join(parts)


def _use_cases_line(agent: dict[str, Any]) -> str:
    cases = agent.get("short_use_cases")
    if not isinstance(cases, list):
        return ""
    cleaned = [str(item).strip() for item in cases if str(item).strip()]
    if not cleaned:
        return ""
    return ", ".join(cleaned[:4])


def _tool_annotations(agent: dict[str, Any]) -> dict[str, Any]:
    tooling_kind = str(agent.get("tooling_kind") or "").strip().lower()
    read_only_kinds = {
        "live_api",
        "live_api_plus_llm",
        "live_fetch_plus_llm",
        "live_network_checks",
        "tool_execution",
        "llm_structured_analysis",
        "hybrid_search",
        "browser_automation",
    }
    read_only = tooling_kind in read_only_kinds
    if str(agent.get("name") or "").strip().lower() in {"shell executor", "python code executor", "multi-file python executor", "multi-language executor"}:
        read_only = False
    return {
        "readOnlyHint": read_only,
        "destructiveHint": False,
        "openWorldHint": True,
        "idempotentHint": bool(read_only and agent.get("cacheable")),
    }


def _example_snippet(agent: dict[str, Any]) -> str:
    """Return a short inline work example from output_examples, if available."""
    examples = agent.get("output_examples")
    if not isinstance(examples, list) or not examples:
        return ""
    ex = examples[0]
    if not isinstance(ex, dict):
        return ""
    # Try to surface the output summary, falling back to full repr
    output = ex.get("output") or ex.get("result") or ex.get("summary")
    if not output:
        return ""
    snippet = str(output)
    if len(snippet) > 200:
        snippet = snippet[:197] + "..."
    return snippet


_USE_WHEN_PREFIXES = ("use when", "use this when", "use to ", "call when", "call this when")


def _normalize_description_for_claude(name: str, description: str) -> str:
    """Ensure the description starts with actionable 'Use when' guidance that tells
    Claude Code exactly when to invoke this tool without being asked.

    Third-party agents may have vague descriptions like "A tool that does X."
    We reframe those to "Use this when you need to do X." so Claude's tool-selection
    works correctly without the user having to spell it out.
    """
    if not description:
        return f"Use this when you need {name.lower()}."

    lower = description.lower().strip()
    if any(lower.startswith(prefix) for prefix in _USE_WHEN_PREFIXES):
        return description  # already action-framed

    # Vague descriptions: reframe with "Use this when you need to..."
    # Strip leading filler phrases before reframing
    for filler in ("this agent ", "an agent that ", "a tool that ", "this tool "):
        if lower.startswith(filler):
            description = description[len(filler):].strip()
            # Capitalize first char
            description = description[0].upper() + description[1:]
            break

    return f"Use this when you need to: {description}"


def build_mcp_tool_entries(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert agent registry listings to MCP tool entry format.

    Returns a list of ``{agent_id, tool: {name, description, input_schema}}``
    dicts. Names are deduplicated by appending a counter suffix when collisions
    occur. Agents without a valid ``agent_id`` are skipped.
    """
    entries: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for agent in agents:
        agent_id = str(agent.get("agent_id") or "").strip()
        if not agent_id:
            continue
        name = str(agent.get("name") or "").strip() or f"Agent {agent_id[:8]}"
        raw_description = str(agent.get("description") or "").strip()

        # Normalize the description so Claude knows when to use this tool
        action_description = _normalize_description_for_claude(name, raw_description)
        tool_description = f"{name}: {action_description}"

        # Append quality signals so Claude can pick the best agent when multiple match
        catalog = _catalog_line(agent)
        if catalog:
            tool_description = f"{tool_description}\nCatalog: {catalog}"
        quality = _quality_line(agent)
        if quality:
            tool_description = f"{tool_description}\n\nQuality: {quality}"
        privacy = _privacy_line(agent)
        if privacy:
            tool_description = f"{tool_description}\nPrivacy: {privacy}"
        use_cases = _use_cases_line(agent)
        if use_cases:
            tool_description = f"{tool_description}\nBest for: {use_cases}"
        example = _example_snippet(agent)
        if example:
            tool_description = f"{tool_description}\nExample output: {example}"

        tool_name = _tool_name(agent, used_names)
        input_schema = normalize_schema(agent.get("input_schema"))
        output_schema = normalize_schema(agent.get("output_schema"))

        # Inject description into each property that lacks one, so Claude can fill
        # arguments correctly without calling aztea_describe first. Shallow-copy each
        # modified property dict to avoid mutating the shared spec objects.
        props = input_schema.get("properties") or {}
        if props:
            new_props: dict[str, Any] = {}
            for prop_name, prop_schema in props.items():
                if isinstance(prop_schema, dict) and not prop_schema.get("description"):
                    prop_schema = {**prop_schema, "description": f"{prop_name} parameter for {name}"}
                new_props[prop_name] = prop_schema
            input_schema = {**input_schema, "properties": new_props}

        tool = {
            "name": tool_name,
            "description": tool_description,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "annotations": _tool_annotations(agent),
        }
        entries.append(
            {
                "agent_id": agent_id,
                "tool_name": tool_name,
                "tool": tool,
                "catalog_metadata": {
                    "name": name,
                    "category": agent.get("category"),
                    "tags": list(agent.get("tags") or []),
                    "is_featured": bool(agent.get("is_featured", False)),
                    "cacheable": bool(agent.get("cacheable", False)),
                    "runtime_requirements": list(agent.get("runtime_requirements") or []),
                    "tooling_kind": agent.get("tooling_kind"),
                    "stability_tier": agent.get("stability_tier"),
                    "codex_recommended": bool(agent.get("codex_recommended", False)),
                    "short_use_cases": list(agent.get("short_use_cases") or []),
                    "trust_score": agent.get("trust_score"),
                    "success_rate": agent.get("success_rate"),
                    "avg_latency_ms": agent.get("avg_latency_ms"),
                    "price_per_call_usd": agent.get("price_per_call_usd"),
                    "verified": bool(agent.get("verified", False)),
                },
            }
        )
    return entries


def build_mcp_manifest(agents: list[dict[str, Any]]) -> dict[str, Any]:
    entries = build_mcp_tool_entries(agents)
    tools = [entry["tool"] for entry in entries]
    return {
        "tools": tools,
        "count": len(tools),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
