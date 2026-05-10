"""Per-curated-agent contract surface.

# OWNS: parametrized assertions over CURATED_PUBLIC_BUILTIN_AGENT_IDS.
# INVARIANTS asserted: every curated agent has a spec, an endpoint registered,
#       a snake_case MCP tool name, a normalizable input/output schema, and
#       a non-negative price.
"""
from __future__ import annotations

import re

import pytest

from core.mcp_manifest import build_mcp_tool_entries, normalize_schema
from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS,
    CURATED_PUBLIC_BUILTIN_AGENT_IDS,
)
from server.builtin_agents.specs import builtin_agent_specs

pytestmark = pytest.mark.surface

_SPECS_BY_ID = {s["agent_id"]: s for s in builtin_agent_specs()}
_CURATED = list(CURATED_PUBLIC_BUILTIN_AGENT_IDS)
_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


def _agent_id(aid: str) -> str:
    """Pretty test ID — use the agent's slug/name when available."""
    spec = _SPECS_BY_ID.get(aid)
    return spec["name"] if spec else aid[:8]


_PARAMS = [pytest.param(aid, id=_agent_id(aid)) for aid in _CURATED]


@pytest.mark.parametrize("agent_id", _PARAMS)
def test_curated_has_spec(agent_id):
    assert agent_id in _SPECS_BY_ID, (
        f"curated agent {agent_id} is missing from builtin_agent_specs()"
    )


@pytest.mark.parametrize("agent_id", _PARAMS)
def test_curated_has_endpoint(agent_id):
    assert agent_id in BUILTIN_INTERNAL_ENDPOINTS, (
        f"curated agent {agent_id} not registered in BUILTIN_INTERNAL_ENDPOINTS"
    )


@pytest.mark.parametrize("agent_id", _PARAMS)
def test_curated_input_schema_valid(agent_id):
    spec = _SPECS_BY_ID[agent_id]
    schema = normalize_schema(spec.get("input_schema"))
    assert isinstance(schema, dict)


@pytest.mark.parametrize("agent_id", _PARAMS)
def test_curated_output_schema_valid(agent_id):
    spec = _SPECS_BY_ID[agent_id]
    schema = normalize_schema(spec.get("output_schema"))
    assert isinstance(schema, dict)


@pytest.mark.parametrize("agent_id", _PARAMS)
def test_curated_price_non_negative(agent_id):
    spec = _SPECS_BY_ID[agent_id]
    price = spec.get("price_per_call_usd")
    if price is None:
        return
    assert float(price) >= 0


@pytest.mark.parametrize("agent_id", _PARAMS)
def test_curated_has_description(agent_id):
    spec = _SPECS_BY_ID[agent_id]
    desc = spec.get("description") or ""
    assert len(desc.strip()) > 0


@pytest.mark.parametrize("agent_id", _PARAMS)
def test_curated_has_snake_case_mcp_tool_name(agent_id):
    """Each curated agent surfaces in MCP under a snake_case tool name."""
    entries = build_mcp_tool_entries([_SPECS_BY_ID[agent_id]])
    assert entries, f"build_mcp_tool_entries returned empty for {agent_id}"
    name = entries[0]["tool_name"]
    assert _SNAKE_CASE.match(name), f"non-snake_case: {name!r}"


@pytest.mark.parametrize("agent_id", _PARAMS)
def test_curated_endpoint_url_starts_with_internal_or_skill(agent_id):
    """Curated builtins live behind internal:// or skill:// — never raw HTTP."""
    spec = _SPECS_BY_ID[agent_id]
    endpoint = spec.get("endpoint_url") or ""
    assert endpoint.startswith(("internal://", "skill://")), (
        f"{spec.get('name')} has unexpected endpoint: {endpoint!r}"
    )


@pytest.mark.parametrize("agent_id", _PARAMS)
def test_curated_has_at_least_one_tag(agent_id):
    spec = _SPECS_BY_ID[agent_id]
    tags = spec.get("tags") or []
    assert isinstance(tags, list) and len(tags) > 0, f"{spec.get('name')} has no tags"


@pytest.mark.parametrize("agent_id", _PARAMS)
def test_curated_stability_tier_is_known(agent_id):
    spec = _SPECS_BY_ID[agent_id]
    tier = (spec.get("stability_tier") or "").strip().lower()
    # Empty is acceptable (defaults applied downstream); otherwise must be one of
    # the documented values.
    if tier:
        assert tier in {"stable", "beta", "experimental", "alpha", "deprecated"}, (
            f"{spec.get('name')} has unknown stability_tier: {tier!r}"
        )
