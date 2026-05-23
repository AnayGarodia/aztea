"""
schema_migration_planner.py — D19: zero-downtime migration plan validated
against the actual production query log.

# v0 STATUS: requires query-log sample path. Returns requires_configuration
#   otherwise — guessing about real queries would defeat the agent's point.
# REASONING LOOP: plan intermediate states → synthesise stage-by-stage plan.
"""

from __future__ import annotations

import os
from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "schema_migration_planner"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    current_schema = (payload.get("current_schema") or "").strip()
    target_schema = (payload.get("target_schema") or "").strip()
    query_log_path = (payload.get("query_log_path") or "").strip()
    if not current_schema or not target_schema:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "current_schema and target_schema are both required")
    budget = clamp_int(payload.get("budget_cents"), 50, 1, 500)

    if not query_log_path or not os.path.isfile(query_log_path):
        return requires_configuration(
            _AGENT_SLUG,
            ["query_log_path (must exist on disk)"],
            "The planner verifies each intermediate state against the real "
            "production query log. Without the log, the plan is no better "
            "than what Claude can guess in a chat session.",
            {"current_schema_chars": len(current_schema),
             "target_schema_chars": len(target_schema)},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Enumerate candidate intermediate schemas. Output JSON "
            '{"stages": [{"sql": "...", "rollback": "..."}, ...]}.'
        ),
        plan_user=(
            f"current_chars={len(current_schema)} "
            f"target_chars={len(target_schema)} "
            f"log={query_log_path}"
        ),
        synth_system=(
            "Verify each stage against the query log. Return JSON "
            '{"verified_stages": [...], "blocking_queries": [...]}.'
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:800]}",
        budget_cents=budget,
        extra_output={"query_log_path": query_log_path},
    )
