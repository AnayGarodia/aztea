"""
bisect_and_blame.py — A2: localize a regression to a specific commit.

# v0 STATUS: requires JobLifecycleBackend for parallel bisect.
# REASONING LOOP: plan bisect strategy → synthesise blame after fan-out.
"""

from __future__ import annotations

import os
from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "bisect_and_blame"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    good = (payload.get("good_ref") or "").strip()
    bad = (payload.get("bad_ref") or "").strip()
    repro = (payload.get("repro_cmd") or "").strip()
    if not good or not bad or not repro:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "good_ref, bad_ref, repro_cmd are all required")
    budget = clamp_int(payload.get("budget_cents"), 30, 1, 500)

    if os.environ.get("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED") != "1":
        return requires_configuration(
            _AGENT_SLUG,
            ["AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED=1"],
            "Bisect-and-Blame needs the cross-process runner backend to "
            "run benchmark trials in parallel across ~log2(N) commits.",
            {"good_ref": good, "bad_ref": bad},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "You design a git-bisect strategy. Given a good ref, bad ref, "
            "and a reproducer command, output JSON {midpoint_strategy, "
            "noise_tolerance, max_rounds}."
        ),
        plan_user=f"good={good} bad={bad} repro={repro}",
        synth_system=(
            "Summarise the bisect outcome. Return JSON "
            '{"blamed_commit": "...", "rationale": "..."}'
        ),
        synth_user_builder=lambda plan: f"Plan was: {plan[:600]}",
        budget_cents=budget,
        extra_output={"good_ref": good, "bad_ref": bad},
    )
