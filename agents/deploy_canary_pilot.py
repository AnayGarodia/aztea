"""
deploy_canary_pilot.py — A3: ship a canary deploy and roll back on SLO breach.

# v0 STATUS: requires deploy creds + metric source. Returns
#   requires_configuration when not wired. Real-world action — only enable
#   in environments where the deploy is reversible.
# REASONING LOOP: plan canary thresholds → synthesise deploy verdict after
#   the watch window.
"""

from __future__ import annotations

import os
from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "deploy_canary_pilot"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    deploy_cmd = (payload.get("deploy_cmd") or "").strip()
    slo_thresholds = payload.get("slo_thresholds")
    watch_seconds = clamp_int(payload.get("watch_seconds"), 1800, 60, 14400)
    if not deploy_cmd:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "deploy_cmd is required")
    if not isinstance(slo_thresholds, dict) or not slo_thresholds:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "slo_thresholds must be a non-empty dict")
    budget = clamp_int(payload.get("budget_cents"), 30, 1, 500)

    missing: list[str] = []
    if not os.environ.get("AZTEA_DEPLOY_API_TOKEN"):
        missing.append("AZTEA_DEPLOY_API_TOKEN")
    if not os.environ.get("AZTEA_METRICS_API_URL"):
        missing.append("AZTEA_METRICS_API_URL")
    if missing:
        return requires_configuration(
            _AGENT_SLUG, missing,
            "Deploy Canary Pilot needs both a deploy-API token and a "
            "metrics endpoint to poll. Without these the agent cannot "
            "honestly attest to the canary's behaviour.",
            {"deploy_cmd": deploy_cmd, "watch_seconds": watch_seconds},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Design a canary verdict policy. Output JSON "
            '{"abort_rules": [...], "promote_rules": [...]}.'
        ),
        plan_user=f"thresholds={slo_thresholds} watch={watch_seconds}s",
        synth_system=(
            "Issue the canary verdict. Return JSON "
            '{"verdict": "promoted|rolled_back|inconclusive", '
            '"rationale": "..."}'
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:600]}",
        budget_cents=budget,
        extra_output={"deploy_cmd": deploy_cmd, "watch_seconds": watch_seconds},
    )
