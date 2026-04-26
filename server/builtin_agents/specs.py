"""Compose built-in agent registration specs from split modules."""

from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import CURATED_BUILTIN_AGENT_IDS
from server.builtin_agents.specs_part1 import load_builtin_specs_part1
from server.builtin_agents.specs_part2 import load_builtin_specs_part2
from server.builtin_agents.specs_part3 import load_builtin_specs_part3


def builtin_agent_specs() -> list[dict[str, Any]]:
    specs = load_builtin_specs_part1()
    specs.extend(load_builtin_specs_part2())
    specs.extend(load_builtin_specs_part3())
    return [spec for spec in specs if spec.get("agent_id") in CURATED_BUILTIN_AGENT_IDS]
