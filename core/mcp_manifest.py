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
    raw_type = (
        str(field.get("type") or field.get("input_type") or "string").strip().lower()
    )
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
        key in schema
        for key in ("type", "properties", "items", "oneOf", "allOf", "anyOf")
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


_LATENCY_SECONDS_THRESHOLD_MS = 1000
_CLIENT_TRUST_PREVIEW = 3


def _format_reputation_parts(agent: dict[str, Any]) -> list[str]:
    """Pure: shape trust/success/latency/calls signals into display strings."""
    parts: list[str] = []
    if agent.get("verified"):
        parts.append("verified")
    trust = agent.get("trust_score")
    if trust is not None:
        parts.append(f"trust {int(trust)}/100")
    success = agent.get("success_rate")
    if success is not None:
        parts.append(f"{int(round(float(success) * 100))}% success")
    latency = agent.get("avg_latency_ms")
    if latency is not None and float(latency) > 0:
        ms = float(latency)
        parts.append(
            f"~{ms / 1000:.1f}s avg" if ms >= _LATENCY_SECONDS_THRESHOLD_MS else f"~{int(ms)}ms avg"
        )
    calls = agent.get("total_calls")
    if calls is not None and int(calls) > 0:
        parts.append(f"{int(calls):,} calls")
    return parts


def _format_client_trust(by_client: Any) -> str | None:
    """Pure: top-N client trust scores joined into one display string, or None if empty."""
    if not isinstance(by_client, dict) or not by_client:
        return None
    ranked = sorted(
        (
            (str(client_id), float(score))
            for client_id, score in by_client.items()
            if client_id and score is not None
        ),
        key=lambda item: item[1],
        reverse=True,
    )[:_CLIENT_TRUST_PREVIEW]
    if not ranked:
        return None
    labels = ", ".join(f"{client_id} {int(score)}" for client_id, score in ranked)
    return f"client trust: {labels}"


def _format_price_part(agent: dict[str, Any]) -> str | None:
    """Pure: pricing-model-aware price string, or None when no price is set."""
    price = agent.get("price_per_call_usd")
    if price is None:
        return None
    pricing_model = str(agent.get("pricing_model") or "fixed").lower()
    if pricing_model == "per_unit":
        return f"${float(price):.3f}/unit (variable)"
    if pricing_model == "tiered":
        return f"from ${float(price):.3f}/call (tiered)"
    return f"${float(price):.3f}/call"


def _quality_line(agent: dict[str, Any]) -> str:
    """Pure: one-line quality + pricing summary suitable for an MCP tool description."""
    parts = _format_reputation_parts(agent)
    client_trust = _format_client_trust(agent.get("by_client"))
    if client_trust:
        parts.append(client_trust)
    price_part = _format_price_part(agent)
    if price_part:
        parts.append(price_part)
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
    if str(agent.get("name") or "").strip().lower() in {
        "shell executor",
        "python code executor",
        "multi-file python executor",
        "multi-language executor",
    }:
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


_USE_WHEN_PREFIXES = (
    "use when",
    "use this when",
    "use to ",
    "call when",
    "call this when",
)


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
            description = description[len(filler) :].strip()
            # Capitalize first char
            description = description[0].upper() + description[1:]
            break

    return f"Use this when you need to: {description}"


def _compose_tool_description(name: str, agent: dict[str, Any]) -> str:
    """Pure: build the multi-line MCP tool description from agent metadata.

    Why: each suffix line is optional; assembling them here keeps the
    ``build_mcp_tool_entries`` orchestrator small and easy to read.
    """
    raw_description = str(agent.get("description") or "").strip()
    action = _normalize_description_for_claude(name, raw_description)
    description = f"{name}: {action}"
    catalog = _catalog_line(agent)
    if catalog:
        description = f"{description}\nCatalog: {catalog}"
    quality = _quality_line(agent)
    if quality:
        description = f"{description}\n\nQuality: {quality}"
    privacy = _privacy_line(agent)
    if privacy:
        description = f"{description}\nPrivacy: {privacy}"
    use_cases = _use_cases_line(agent)
    if use_cases:
        description = f"{description}\nBest for: {use_cases}"
    example = _example_snippet(agent)
    if example:
        description = f"{description}\nExample output: {example}"
    return description


def _annotate_input_schema(input_schema: dict[str, Any], agent_name: str) -> dict[str, Any]:
    """Pure: shallow-copy and inject default property descriptions.

    Why: Claude fills arguments correctly without calling aztea_describe
    first; shallow-copying prevents mutation of shared spec objects.
    """
    props = input_schema.get("properties") or {}
    if not props:
        return input_schema
    new_props: dict[str, Any] = {}
    for prop_name, prop_schema in props.items():
        if isinstance(prop_schema, dict) and not prop_schema.get("description"):
            prop_schema = {
                **prop_schema,
                "description": f"{prop_name} parameter for {agent_name}",
            }
        new_props[prop_name] = prop_schema
    return {**input_schema, "properties": new_props}


def _build_catalog_metadata(
    agent: dict[str, Any], name: str,
    required_fields: list[str], input_fields: list[str],
) -> dict[str, Any]:
    """Pure: project agent fields into the catalog metadata block."""
    return {
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
        "required_fields": required_fields,
        "input_fields": input_fields,
        "pricing_model": agent.get("pricing_model"),
        "pricing_config": agent.get("pricing_config"),
    }


def _build_one_entry(
    agent: dict[str, Any], used_names: set[str],
) -> dict[str, Any] | None:
    """Pure-ish: shape one agent into an MCP tool entry; ``None`` if missing ``agent_id``.

    Mutates ``used_names`` to track collisions across the manifest.
    """
    agent_id = str(agent.get("agent_id") or "").strip()
    if not agent_id:
        return None
    name = str(agent.get("name") or "").strip() or f"Agent {agent_id[:8]}"
    tool_description = _compose_tool_description(name, agent)
    tool_name = _tool_name(agent, used_names)
    input_schema = _annotate_input_schema(normalize_schema(agent.get("input_schema")), name)
    output_schema = normalize_schema(agent.get("output_schema"))
    input_fields = sorted((input_schema.get("properties") or {}).keys())
    required_fields = list(input_schema.get("required") or [])
    return {
        "agent_id": agent_id,
        "tool_name": tool_name,
        "tool": {
            "name": tool_name,
            "description": tool_description,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "annotations": _tool_annotations(agent),
        },
        "catalog_metadata": _build_catalog_metadata(agent, name, required_fields, input_fields),
    }


def build_mcp_tool_entries(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure: shape registry listings into MCP tool entries; deduplicates tool names."""
    used_names: set[str] = set()
    entries: list[dict[str, Any]] = []
    for agent in agents:
        entry = _build_one_entry(agent, used_names)
        if entry is not None:
            entries.append(entry)
    return entries


def build_mcp_manifest(agents: list[dict[str, Any]]) -> dict[str, Any]:
    entries = build_mcp_tool_entries(agents)
    tools = [entry["tool"] for entry in entries]
    return {
        "tools": tools,
        "count": len(tools),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
