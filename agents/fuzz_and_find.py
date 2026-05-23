"""
fuzz_and_find.py — B6: find counterexamples to a stated property.

# v0 STATUS: requires JobLifecycleBackend for parallel fuzzing fan-out.
# Today the more specific quant_patch_validator agent handles
# differential fuzzing for quant code; this is the generalisation.
# REASONING LOOP: plan input generator → synthesise counterexample report.
"""

from __future__ import annotations

import os
from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "fuzz_and_find"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    func_source = (payload.get("function_source") or "").strip()
    property_spec = (payload.get("property_spec") or "").strip()
    if not func_source or not property_spec:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "function_source and property_spec are both required")
    iterations = clamp_int(payload.get("iterations"), 10_000, 100, 1_000_000)
    budget = clamp_int(payload.get("budget_cents"), 40, 1, 500)

    if os.environ.get("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED") != "1":
        return requires_configuration(
            _AGENT_SLUG, ["AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED=1"],
            "Fuzz-and-Find needs the cross-process runner to fan out fuzz "
            "trials across workers; in-process exec would saturate one thread.",
            {"iterations": iterations,
             "function_source_chars": len(func_source)},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Plan a Hypothesis-style input strategy for the function. "
            "Output JSON {strategy_outline, seed_count, shrink_policy}."
        ),
        plan_user=f"Source: {func_source[:1500]}\nProperty: {property_spec}\n"
                  f"Iterations: {iterations}",
        synth_system=(
            "Given fuzz outcomes, return JSON "
            '{"counterexample": "...", "minimal_repro": "..."}'
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:800]}",
        budget_cents=budget,
        extra_output={"iterations": iterations},
    )
