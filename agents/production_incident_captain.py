"""
production_incident_captain.py — C15: coordinate the first 30 min of an incident.

# v0 STATUS: requires PagerDuty + Sentry creds and a writable runbook doc
#   target. Returns requires_configuration otherwise — escalating without
#   a real source of truth would generate noise, not clarity.
# REASONING LOOP: plan hypothesis tree → synthesise war-room doc.
"""

from __future__ import annotations

import os
from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "production_incident_captain"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    page_id = (payload.get("page_id") or "").strip()
    if not page_id:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "page_id is required (PagerDuty incident reference)")
    confidence_threshold = float(payload.get("escalation_confidence_threshold") or 0.7)
    if not (0.0 < confidence_threshold <= 1.0):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "escalation_confidence_threshold must be in (0, 1]")
    budget = clamp_int(payload.get("budget_cents"), 50, 1, 500)

    missing: list[str] = []
    for v in ("PAGERDUTY_API_TOKEN", "SENTRY_API_TOKEN"):
        if not os.environ.get(v):
            missing.append(v)
    if not os.environ.get("AZTEA_INCIDENT_DOC_TARGET"):
        missing.append("AZTEA_INCIDENT_DOC_TARGET")
    if missing:
        return requires_configuration(
            _AGENT_SLUG, missing,
            "Incident captain needs live alert sources and a doc target. "
            "Escalating from a stub would generate noise, not clarity.",
            {"page_id": page_id},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Plan the first-30-min response. Output JSON "
            '{"hypothesis_tree_root": "...", "data_pulls": [...]}'
        ),
        plan_user=f"page_id={page_id} threshold={confidence_threshold}",
        synth_system=(
            "Draft the war-room doc + escalation decision. Return JSON "
            '{"doc_url": "...", "should_escalate": true|false, '
            '"top_hypothesis": "..."}'
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:800]}",
        budget_cents=budget,
        extra_output={"page_id": page_id},
    )
