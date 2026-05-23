"""
dmarc_email_verifier.py — C13: pre-flight a real outbound email campaign.

# v0 STATUS: requires SMTP creds and a canary inbox to receive the test
#   send. Returns requires_configuration otherwise.
# REASONING LOOP: plan probe list → synthesise per-domain verdict.
"""

from __future__ import annotations

import os
from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "dmarc_email_verifier"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    sample = payload.get("sample_email")
    targets = payload.get("target_domains")
    if not isinstance(sample, dict) or not sample.get("from") or not sample.get("body"):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "sample_email must include 'from' and 'body'")
    if not isinstance(targets, list) or not targets:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "target_domains must be a non-empty list")
    budget = clamp_int(payload.get("budget_cents"), 20, 1, 500)

    missing: list[str] = []
    for v in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        if not os.environ.get(v):
            missing.append(v)
    if not os.environ.get("AZTEA_DMARC_CANARY_INBOX"):
        missing.append("AZTEA_DMARC_CANARY_INBOX")
    if missing:
        return requires_configuration(
            _AGENT_SLUG, missing,
            "DMARC verifier needs real SMTP credentials and a canary inbox "
            "to send and re-receive the test message.",
            {"target_count": len(targets)},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Plan SPF / DKIM / DMARC probes per target. Output JSON "
            '{"probes": [...], "blacklists": [...]}.'
        ),
        plan_user=f"from={sample.get('from')} targets={targets}",
        synth_system=(
            "Issue go/no-go per target. Return JSON "
            '{"verdicts": [{"domain": "...", "verdict": "go|no_go", '
            '"reasons": [...]}]}'
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:600]}",
        budget_cents=budget,
        extra_output={"targets": targets},
    )
