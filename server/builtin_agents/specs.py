"""Compose built-in agent registration specs from split modules."""

from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    CURATED_BUILTIN_AGENT_IDS,
    DEPRECATED_BUILTIN_AGENT_IDS,
)
from server.builtin_agents.specs_part1 import load_builtin_specs_part1
from server.builtin_agents.specs_part2 import load_builtin_specs_part2
from server.builtin_agents.specs_part3 import load_builtin_specs_part3


def builtin_agent_specs() -> list[dict[str, Any]]:
    specs = load_builtin_specs_part1()
    specs.extend(load_builtin_specs_part2())
    specs.extend(load_builtin_specs_part3())
    result = []
    for spec in specs:
        agent_id = spec.get("agent_id")
        if agent_id in CURATED_BUILTIN_AGENT_IDS:
            result.append(spec)
        elif agent_id in DEPRECATED_BUILTIN_AGENT_IDS:
            # Register deprecated agents normally so existing callers can still
            # invoke them, but mark them deprecated so the registry list can
            # filter them from public discovery.
            result.append({**spec, "deprecated": True})
    return result
