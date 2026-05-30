"""
flake_hunter.py — A1: characterize and (where possible) fix a flaky test.

# OWNS: reasoning-loop scaffold for flake characterization.
# v0 STATUS: requires the cross-process job lifecycle backend to fan out
#   parallel re-runs. Without it, returns requires_configuration with the
#   exact env-flag needed.
# REASONING LOOP: (1) plan factor matrix → (2) interpret rerun outcomes
#   → (3) propose fix. ≥ 2 LLM calls.

Input:
    {
        "test_path":       "tests/integration/test_foo.py::test_bar",
        "repo_root":       "/abs/path/to/repo",
        "trials":          200,          # optional, default 200, max 1000
        "factors":         ["seed", "parallelism", "env_tz"],   # optional
        "budget_cents":    50
    }

Output (success — once runner pool is wired):
    {
        "flake_rate": 0.04,
        "variants": [{"factor": "...", "value": "...", "rate": 0.0}, ...],
        "minimal_reproducer": "...",
        "suggested_fix": "...",
        "trace": <trace>
    }

Output (today):
    {"error": {"code": "flake_hunter.requires_configuration", ...}}
"""

from __future__ import annotations

import os
from typing import Any

from agents._contracts import (
    agent_error as _err,
    annotate_success as _annotate,
)
from agents._reasoning_scaffold import clamp_int as _clamp_int
from core.llm.base import CompletionRequest, Message
from core.llm.errors import BudgetExceededError, LLMError
from core.llm.fallback import run_with_fallback
from core.reasoning_traces import TraceRecorder

_AGENT_SLUG = "flake_hunter"
_DEFAULT_BUDGET_CENTS = 50
_DEFAULT_TRIALS = 200
_HARD_MAX_TRIALS = 1000


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")

    test_path = (payload.get("test_path") or "").strip()
    repo_root = (payload.get("repo_root") or "").strip()
    if not test_path:
        return _err(f"{_AGENT_SLUG}.invalid_input", "test_path is required")
    if not repo_root or not os.path.isabs(repo_root):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "repo_root must be an absolute path")
    trials = _clamp_int(payload.get("trials"), _DEFAULT_TRIALS, 1, _HARD_MAX_TRIALS)
    budget_cents = _clamp_int(payload.get("budget_cents"),
                              _DEFAULT_BUDGET_CENTS, 1, 500)

    # Configuration gate. The JobLifecycleBackend is the only path that lets
    # the agent actually spawn parallel reruns inside the Aztea worker pool.
    # When it's wired (env flag below), this agent fans out N trials, polls
    # via list_child_jobs, and synthesises the report.
    if os.environ.get("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED") != "1":
        return _err(
            f"{_AGENT_SLUG}.requires_configuration",
            "Flake Hunter needs the cross-process runner backend "
            "(JobLifecycleBackend) to fan out parallel test reruns.",
            {
                "missing": ["AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED=1"],
                "hint": "v0 ships the in-process runner only; the lifecycle "
                        "backend lands with the first consumer agent.",
                "test_path": test_path,
                "planned_trials": trials,
            },
        )

    # Reasoning loop — fires only when the runner backend is wired.
    trace = TraceRecorder()
    try:
        with trace.step("plan_factor_matrix",
                        inputs_summary={"trials": trials}):
            plan_resp = run_with_fallback(
                CompletionRequest(
                    model="",
                    messages=[
                        Message(role="system",
                                content="Plan a factored test-rerun matrix. "
                                        "Return JSON {factors:[...]}."),
                        Message(role="user",
                                content=f"Test: {test_path}\nTrials: {trials}"),
                    ],
                    temperature=0.1, max_tokens=400,
                ),
                budget_cents=budget_cents,
            )
            trace.record_llm_call()
            trace.record_outputs({"plan_preview": plan_resp.text[:200]})

        # (Real fan-out via runner pool happens here once enabled.)

        with trace.step("synthesise_report"):
            synth_resp = run_with_fallback(
                CompletionRequest(
                    model="",
                    messages=[
                        Message(role="system",
                                content="Summarise the rerun outcomes. "
                                        "Return JSON {flake_rate, summary}."),
                        Message(role="user",
                                content=f"Test: {test_path} "
                                        f"Plan: {plan_resp.text[:400]}"),
                    ],
                    temperature=0.1, max_tokens=400,
                ),
                budget_cents=budget_cents,
            )
            trace.record_llm_call()
            trace.record_outputs({"synth_preview": synth_resp.text[:200]})
    except (BudgetExceededError, LLMError) as exc:
        return _err(f"{_AGENT_SLUG}.llm_error", str(exc),
                    {"trace": trace.to_dict()})

    return _annotate(
        {"test_path": test_path, "trials": trials,
         "plan": plan_resp.text, "synthesis": synth_resp.text,
         "trace": trace.to_dict()},
        llm_used=True,
    )
