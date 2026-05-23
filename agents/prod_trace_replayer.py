"""
prod_trace_replayer.py — D18: replay sanitized prod traffic against a candidate
build and report behavior diffs.

# v0 STATUS: requires JobLifecycleBackend + a trace bundle path. Returns
#   requires_configuration otherwise.
# REASONING LOOP: plan replay slices → synthesise diff report.
"""

from __future__ import annotations

import os
from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "prod_trace_replayer"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    candidate_url = (payload.get("candidate_url") or "").strip()
    bundle_path = (payload.get("trace_bundle_path") or "").strip()
    if not candidate_url:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "candidate_url is required (HTTP target to replay against)")
    if not bundle_path:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "trace_bundle_path is required")
    budget = clamp_int(payload.get("budget_cents"), 40, 1, 500)

    missing: list[str] = []
    if os.environ.get("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED") != "1":
        missing.append("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED=1")
    if not os.path.isfile(bundle_path):
        missing.append(f"trace_bundle_path={bundle_path} (file must exist)")
    if missing:
        return requires_configuration(
            _AGENT_SLUG, missing,
            "Trace replayer needs the lifecycle runner for parallel replay "
            "and a real trace bundle on disk.",
            {"candidate_url": candidate_url, "bundle_path": bundle_path},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Plan trace replay slices. Output JSON "
            '{"slice_by": ["route", "method"], "sample_rate": 0.1}'
        ),
        plan_user=f"target={candidate_url}",
        synth_system=(
            "Synthesise the behaviour-diff report. Return JSON "
            '{"diffs_by_route": [...], "regression_count": 0}.'
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:600]}",
        budget_cents=budget,
        extra_output={"candidate_url": candidate_url},
    )
