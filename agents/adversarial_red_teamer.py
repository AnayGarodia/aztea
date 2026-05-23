"""
adversarial_red_teamer.py — E22: attempt to break an endpoint.

# v0 STATUS: requires explicit authorization (per-target consent token)
#   AND the JobLifecycleBackend for parallel probing. Refuses to run
#   without both — running unauthorized red-team probes is a legal
#   landmine, not an opt-out we can hide behind config.
# REASONING LOOP: plan attack matrix → synthesise findings or
#   no-exploit-found report.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "adversarial_red_teamer"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    target_url = (payload.get("target_url") or "").strip()
    goal = (payload.get("goal") or "").strip()
    consent_token = (payload.get("consent_token") or "").strip()
    if not target_url:
        return _err(f"{_AGENT_SLUG}.invalid_input", "target_url is required")
    if urlparse(target_url).scheme not in {"http", "https"}:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "target_url must be HTTP/HTTPS")
    if not goal:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "goal is required (what would constitute a finding)")
    if not consent_token:
        return _err(
            f"{_AGENT_SLUG}.authorization_required",
            "consent_token is required — adversarial probing without "
            "explicit per-target consent is forbidden",
            {"target_url": target_url},
        )
    budget = clamp_int(payload.get("budget_cents"), 80, 1, 1000)

    missing: list[str] = []
    if os.environ.get("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED") != "1":
        missing.append("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED=1")
    if not os.environ.get("AZTEA_REDTEAM_CONSENT_SIGNING_KEY"):
        missing.append("AZTEA_REDTEAM_CONSENT_SIGNING_KEY")
    if missing:
        return requires_configuration(
            _AGENT_SLUG, missing,
            "Red-teamer needs the lifecycle runner (parallel probes) and a "
            "consent-signing key (so each probe carries a verifiable "
            "authorization claim).",
            {"target_url": target_url, "goal_chars": len(goal)},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Enumerate attack categories. Output JSON "
            '{"categories": ["fuzz", "auth", "logic", "race"], '
            '"per_category_payloads": [...]}'
        ),
        plan_user=f"target={target_url} goal={goal[:200]}",
        synth_system=(
            "Return JSON {\"findings\": [{\"poc\": \"...\", "
            "\"severity\": \"...\"}], \"verdict\": \"exploit_found|none_found\"}"
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:800]}",
        budget_cents=budget,
        extra_output={"target_url": target_url, "goal": goal},
    )
