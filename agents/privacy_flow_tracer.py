"""
privacy_flow_tracer.py — E24: produce a runtime data-flow diagram showing
where PII actually went.

# v0 STATUS: requires eBPF or OpenTelemetry access AND a typed PII tag
#   schema. Returns requires_configuration otherwise.
# REASONING LOOP: plan taint propagation → synthesise flow graph.
"""

from __future__ import annotations

import os
from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "privacy_flow_tracer"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    repo_root = (payload.get("repo_root") or "").strip()
    pii_tags = payload.get("pii_tags")
    if not repo_root or not os.path.isabs(repo_root):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "repo_root must be an absolute path")
    if not isinstance(pii_tags, list) or not pii_tags:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "pii_tags must be a non-empty list (e.g. ['ssn','email'])")
    budget = clamp_int(payload.get("budget_cents"), 80, 1, 1000)

    missing: list[str] = []
    if not os.environ.get("AZTEA_OTEL_COLLECTOR_URL"):
        missing.append("AZTEA_OTEL_COLLECTOR_URL")
    if not os.environ.get("AZTEA_EBPF_AGENT_SOCKET"):
        missing.append("AZTEA_EBPF_AGENT_SOCKET")
    if missing:
        return requires_configuration(
            _AGENT_SLUG, missing,
            "Privacy flow tracer mixes static taint with runtime tracing. "
            "Either OTel or eBPF must be reachable; v0 requires at least "
            "one signal source — without it the result would be guesswork.",
            {"repo_root": repo_root, "pii_tags": pii_tags},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Plan taint propagation. Output JSON "
            '{"sources": [...], "sinks": [...], "propagation_rules": [...]}'
        ),
        plan_user=f"repo={repo_root} tags={pii_tags}",
        synth_system=(
            "Return JSON {\"flow_graph\": {...}, \"risky_egress\": [...]}"
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:800]}",
        budget_cents=budget,
        extra_output={"pii_tags": pii_tags},
    )
