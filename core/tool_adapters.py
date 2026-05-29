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

Audience
--------
Each builder accepts ``audience`` — either ``"authenticated"`` (default; the
existing private endpoints) or ``"public"`` (the IP-rate-limited anonymous
``/api/integrations/*-tools.json`` endpoints). ``public`` mode strips fields
that an unauthenticated integrator must not see:

  * ``trust_score_by_client`` / ``by_client``  — per-client trust map; private.
  * ``owner_id``  — the agent owner's user id; private.
  * ``review_status``  — the listing's moderation status; operator-only.

Public mode also adds a per-tool ``metadata.schema_version`` so integrators
can pin to a known shape, and a top-level ``deprecated_tools`` list so any
upcoming removal can be signalled without breaking the array.
"""

from __future__ import annotations

from typing import Any, Literal

from core import mcp_manifest
# 1.6.3: meta_tools moved from scripts/ to sdks/python-sdk/aztea/mcp/
# (PR #38 consolidation). The prod uvicorn venv does NOT pip-install the
# local SDK, so a bare `from aztea.mcp import ...` fails at boot. Add the
# SDK directory to sys.path before importing — same trick used elsewhere
# for vendored modules. Locally and in tests this is a no-op because
# pytest's conftest already places sdks/python-sdk on the path.
import sys as _sys
from pathlib import Path as _Path
_SDK_DIR = str(_Path(__file__).resolve().parents[1] / "sdks" / "python-sdk")
if _SDK_DIR not in _sys.path:
    _sys.path.insert(0, _SDK_DIR)
from aztea.mcp import meta_tools  # noqa: E402 — sys.path mutation above


Audience = Literal["public", "authenticated"]

# WHY: bumped whenever a public-manifest field is renamed, removed, or
# given different semantics. Integrators pin via ``?version=YYYY-MM-DD``;
# anything older returns 400. Append-only fields don't require a bump.
PUBLIC_MANIFEST_SCHEMA_VERSION = "2026-05-26"


def _catalog_entries(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    registry_entries = mcp_manifest.build_mcp_tool_entries(agents)
    catalog: list[dict[str, Any]] = []

    for tool in meta_tools.get_meta_tools():
        catalog.append(
            {
                "name": str(tool.get("name") or "").strip(),
                "description": str(tool.get("description") or "").strip(),
                "input_schema": tool.get("input_schema")
                or {"type": "object", "properties": {}, "required": []},
                "kind": "meta_tool",
                "agent_id": None,
                "agent": None,
            }
        )

    for entry in registry_entries:
        tool = entry.get("tool") or {}
        agent_id = str(entry.get("agent_id") or "").strip() or None
        agent = next(
            (
                item
                for item in agents
                if str(item.get("agent_id") or "").strip() == agent_id
            ),
            None,
        )
        catalog.append(
            {
                "name": str(tool.get("name") or "").strip(),
                "description": str(tool.get("description") or "").strip(),
                "input_schema": tool.get("input_schema")
                or {"type": "object", "additionalProperties": True},
                "kind": "registry_agent",
                "agent_id": agent_id,
                "agent": agent,
            }
        )

    return [item for item in catalog if item.get("name")]


def _is_public(audience: Audience) -> bool:
    return audience == "public"


def _public_envelope_fields() -> dict[str, Any]:
    """Pure: top-level fields injected into every public manifest.

    Kept here so all three builders return an identical contract for the
    fields integrators rely on for versioning + deprecation signalling.
    """
    return {
        "metadata": {"schema_version": PUBLIC_MANIFEST_SCHEMA_VERSION},
        # WHY: empty today, but the field is reserved so a future cull can
        # warn integrators inline without a separate API call. Shape:
        # ``[{name: str, sunset_date: "YYYY-MM-DD", successor: str | None}]``.
        "deprecated_tools": [],
    }


def build_openai_chat_manifest(
    agents: list[dict[str, Any]],
    audience: Audience = "authenticated",
) -> dict[str, Any]:
    """Build an OpenAI Chat Completions ``tools`` array from the agent registry.

    Each tool is a ``{type: "function", function: {name, description, parameters,
    metadata}}`` object. The ``metadata`` field carries Aztea-specific data
    (price, trust score, privacy flags) that OpenAI passes through opaquely.

    Returns ``{tools, count, tool_format, meta_tools_included, tool_lookup}``.
    In ``audience="public"`` mode also includes ``metadata.schema_version``
    and ``deprecated_tools``.
    """
    public = _is_public(audience)
    tools: list[dict[str, Any]] = []
    tool_lookup: dict[str, dict[str, Any]] = {}
    for item in _catalog_entries(agents):
        metadata: dict[str, Any] = {"aztea_tool_kind": item["kind"]}
        if public:
            metadata["schema_version"] = PUBLIC_MANIFEST_SCHEMA_VERSION
        if item["agent_id"]:
            metadata["aztea_agent_id"] = item["agent_id"]
        agent = item.get("agent") or {}
        if agent:
            metadata["price_per_call_usd"] = float(agent.get("price_per_call_usd") or 0)
            metadata["trust_score"] = agent.get("trust_score")
            metadata["success_rate"] = agent.get("success_rate")
            # 2026-05-18: forwarding has_call_history so downstream tool-format
            # consumers can de-emphasize cold-start agents whose success_rate
            # defaults to 1.0. The ranker (session D) reads this flag.
            metadata["has_call_history"] = bool(agent.get("has_call_history", False))
            if not public:
                # WHY: by_client is a per-caller trust map. Surfacing it to
                # anonymous integrators would leak ranking signal that
                # private callers have paid for. Public manifests omit it.
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
        lookup_entry: dict[str, Any] = {
            "kind": item["kind"],
            "agent_id": item["agent_id"],
            "privacy": {
                "pii_safe": bool(agent.get("pii_safe")) if agent else False,
                "outputs_not_stored": bool(agent.get("outputs_not_stored"))
                if agent
                else False,
                "audit_logged": bool(agent.get("audit_logged")) if agent else False,
                "region_locked": agent.get("region_locked") if agent else None,
            },
        }
        tool_lookup[item["name"]] = lookup_entry
    payload: dict[str, Any] = {
        "tools": tools,
        "count": len(tools),
        "tool_format": "openai_chat_completions",
        "meta_tools_included": True,
        "tool_lookup": tool_lookup,
    }
    if public:
        payload.update(_public_envelope_fields())
    return payload


def build_openai_responses_manifest(
    agents: list[dict[str, Any]],
    audience: Audience = "authenticated",
) -> dict[str, Any]:
    """Build an OpenAI Responses API ``tools`` array (flat function objects).

    Uses the ``{type: "function", name, description, parameters, strict: False}``
    shape required by the Responses API (different from Chat Completions).
    Returns ``{tools, count, tool_format, meta_tools_included, tool_lookup}``.
    In ``audience="public"`` mode also includes ``metadata.schema_version``
    and ``deprecated_tools`` at the top level; ``tool_lookup`` omits
    ``trust_score_by_client``.
    """
    public = _is_public(audience)
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
        lookup_entry: dict[str, Any] = {
            "kind": item["kind"],
            "agent_id": item["agent_id"],
        }
        if not public:
            lookup_entry["trust_score_by_client"] = (
                (item.get("agent") or {}).get("by_client") or {}
            )
        tool_lookup[item["name"]] = lookup_entry
    payload: dict[str, Any] = {
        "tools": tools,
        "count": len(tools),
        "tool_format": "openai_responses_function",
        "meta_tools_included": True,
        "tool_lookup": tool_lookup,
    }
    if public:
        payload.update(_public_envelope_fields())
    return payload


def build_gemini_manifest(
    agents: list[dict[str, Any]],
    audience: Audience = "authenticated",
) -> dict[str, Any]:
    """Build a Gemini ``functionDeclarations`` tool list.

    Returns the canonical Gemini shape:
    ``{tools: [{functionDeclarations: [...]}], function_declarations,
    count, tool_format, meta_tools_included, tool_lookup}``.
    The top-level ``function_declarations`` key is a convenience copy for
    callers that need the flat list without unwrapping the outer envelope.
    In ``audience="public"`` mode also includes ``metadata.schema_version``
    and ``deprecated_tools``; ``tool_lookup`` omits ``trust_score_by_client``.
    """
    public = _is_public(audience)
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
        lookup_entry: dict[str, Any] = {
            "kind": item["kind"],
            "agent_id": item["agent_id"],
        }
        if not public:
            lookup_entry["trust_score_by_client"] = (
                (item.get("agent") or {}).get("by_client") or {}
            )
        tool_lookup[item["name"]] = lookup_entry
    payload: dict[str, Any] = {
        "tools": [{"functionDeclarations": declarations}],
        "function_declarations": declarations,
        "count": len(declarations),
        "tool_format": "gemini_function_declarations",
        "meta_tools_included": True,
        "tool_lookup": tool_lookup,
    }
    if public:
        payload.update(_public_envelope_fields())
    return payload


def build_anthropic_manifest(agents: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an Anthropic Messages-API ``tools`` list.

    Anthropic's tool-use API expects ``[{"name", "description",
    "input_schema"}]`` — a flat list, no nested envelope. Returns:

        {tools, count, tool_format, meta_tools_included, tool_lookup}

    The lookup table is structurally identical to the OpenAI / Gemini
    builders so callers can reuse the same dispatch path. Wave 2
    (2026-05-26) added this so the new ``aztea-anthropic`` package can
    expose the marketplace catalog to Anthropic's Messages API and the
    upcoming Anthropic Agents SDK without re-implementing manifest
    construction on the SDK side.
    """
    tools: list[dict[str, Any]] = []
    tool_lookup: dict[str, dict[str, Any]] = {}
    for item in _catalog_entries(agents):
        tools.append(
            {
                "name": item["name"],
                "description": item["description"],
                "input_schema": item["input_schema"],
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
        "tool_format": "anthropic_tools",
        "meta_tools_included": True,
        "tool_lookup": tool_lookup,
    }


# Fields scrubbed from every agent record before it reaches a public
# manifest builder. Used by the anonymous-endpoint route to ensure no
# admin-poisoned cache leak nor reputation-by-client signal escapes.
_PUBLIC_AGENT_SCRUB_FIELDS = (
    "owner_id",
    "review_status",
    "by_client",
)


def scrub_agents_for_public(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure: copy each agent record dropping fields that must never reach anon callers.

    Defense in depth: even if the manifest builder is called with the
    default ``audience="authenticated"`` by mistake, the input itself
    will not carry the leaked fields. New private fields added to the
    enriched-agent shape must also land here.
    """
    scrubbed: list[dict[str, Any]] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        clean = {k: v for k, v in agent.items() if k not in _PUBLIC_AGENT_SCRUB_FIELDS}
        scrubbed.append(clean)
    return scrubbed
