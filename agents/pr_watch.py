"""
pr_watch.py — A5: babysit a PR for up to 24h.

# v0 STATUS: requires GitHub App install (re-uses
#   core/hosted_index/github_app for token issuance) + the
#   JobLifecycleBackend for the long watch window.
# REASONING LOOP: plan watch rules → synthesise final report.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)
from core.hosted_index import github_app as _github_app

_AGENT_SLUG = "pr_watch"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    pr_url = (payload.get("pr_url") or "").strip()
    if not pr_url:
        return _err(f"{_AGENT_SLUG}.invalid_input", "pr_url is required")
    parsed = urlparse(pr_url)
    if parsed.scheme not in {"http", "https"} or "github.com" not in (parsed.netloc or ""):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "pr_url must be a github.com URL")
    watch_seconds = clamp_int(payload.get("watch_seconds"), 14400, 60, 86400)
    budget = clamp_int(payload.get("budget_cents"), 30, 1, 500)

    missing: list[str] = []
    if not _github_app.is_configured():
        missing.append("GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH")
    if os.environ.get("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED") != "1":
        missing.append("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED=1")
    if missing:
        return requires_configuration(
            _AGENT_SLUG, missing,
            "PR Watch is a longitudinal agent — it needs the GitHub App for "
            "API reads and the lifecycle runner for hour-scale watching.",
            {"pr_url": pr_url, "watch_seconds": watch_seconds},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Plan PR watch rules. Output JSON {poll_interval_s, "
            "rerun_on_infra_blip, ping_owner_when}."
        ),
        plan_user=f"PR: {pr_url} watch={watch_seconds}s",
        synth_system=(
            "Summarise the watch session. Return JSON "
            '{"events": [...], "final_state": "merged|abandoned|stalled"}.'
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:600]}",
        budget_cents=budget,
        extra_output={"pr_url": pr_url, "watch_seconds": watch_seconds},
    )
