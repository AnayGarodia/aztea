"""
migration_pilot.py — A4: run a long DB migration safely on a replica.

# v0 STATUS: requires replica DSN + permission to run DDL there.
# REASONING LOOP: plan strategy (CONCURRENTLY / table-swap / batched) →
#   synthesise runbook after observing the dry run.
"""

from __future__ import annotations

import os
from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "migration_pilot"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    target_sql = (payload.get("target_sql") or "").strip()
    if not target_sql:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "target_sql is required")
    if "DROP" in target_sql.upper() and not payload.get("allow_drops"):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "target_sql contains DROP; set allow_drops=true to confirm")
    lock_threshold_ms = clamp_int(payload.get("lock_threshold_ms"), 5000, 100, 600_000)
    budget = clamp_int(payload.get("budget_cents"), 40, 1, 500)

    missing: list[str] = []
    if not os.environ.get("AZTEA_MIGRATION_REPLICA_DSN"):
        missing.append("AZTEA_MIGRATION_REPLICA_DSN")
    if missing:
        return requires_configuration(
            _AGENT_SLUG, missing,
            "Migration Pilot must run against a replica DSN to dry-run the "
            "migration without touching production.",
            {"target_sql_len": len(target_sql)},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Plan a zero-downtime migration. Output JSON "
            '{"strategy": "concurrent|table_swap|batched", "stages": [...]}.'
        ),
        plan_user=f"SQL: {target_sql[:2000]} lock_threshold_ms={lock_threshold_ms}",
        synth_system=(
            "Produce the migration runbook. Return JSON "
            '{"playbook": [...], "rollback": [...]}.'
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:800]}",
        budget_cents=budget,
        extra_output={"target_sql_chars": len(target_sql)},
    )
