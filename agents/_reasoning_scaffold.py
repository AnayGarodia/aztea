"""
_reasoning_scaffold.py — shared helpers for v0 reasoning-agent stubs.

# OWNS: requires_configuration helper, clamp_int, run a 2-step reasoning
#       loop that exercises ≥ 2 LLM calls + records them in a trace.
# NOT OWNS: per-agent business logic — each agent module owns its own
#           configuration check and structured output shape.
#
# Why this exists: 22 of the 25 new agents follow the same scaffold
# (validate inputs → check required env vars / external creds → if missing,
# return requires_configuration; otherwise run a reasoning loop and return
# structured output). Inlining the boilerplate in each module would
# triple the LOC without adding signal.
"""

from __future__ import annotations

from typing import Any, Callable

from agents._contracts import (
    agent_error as _err,
    annotate_success as _annotate,
)
from core.llm.base import CompletionRequest, Message
from core.llm.errors import BudgetExceededError, LLMError
from core.llm.fallback import run_with_fallback
from core.reasoning_traces import TraceRecorder


def clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """Pure: coerce-or-default into [lo, hi]. Returns ``default`` on bad input."""
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def requires_configuration(
    slug: str,
    missing: list[str],
    hint: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard requires_configuration envelope.

    Why a shared helper: every stub agent needs to surface the same shape
    so the catalog renderer can hint operators uniformly.
    """
    details: dict[str, Any] = {"missing": missing, "hint": hint}
    if extra:
        details.update(extra)
    return _err(
        f"{slug}.requires_configuration",
        f"{slug} requires configuration: {', '.join(missing)}",
        details,
    )


def two_step_reasoning(
    slug: str,
    *,
    plan_system: str,
    plan_user: str,
    synth_system: str,
    synth_user_builder: Callable[[str], str],
    budget_cents: int,
    extra_output: dict[str, Any] | None = None,
    plan_max_tokens: int = 400,
    synth_max_tokens: int = 400,
) -> dict[str, Any]:
    """Run a two-call reasoning loop and return either the structured success
    envelope or the canonical error envelope.

    ``synth_user_builder`` receives the plan response text so the second
    call can be informed by the first (the Section 6.2 reasoning-loop
    requirement).
    """
    trace = TraceRecorder()
    try:
        with trace.step("plan", inputs_summary={"slug": slug}):
            plan_resp = run_with_fallback(
                CompletionRequest(
                    model="",
                    messages=[
                        Message(role="system", content=plan_system),
                        Message(role="user", content=plan_user),
                    ],
                    temperature=0.1, max_tokens=plan_max_tokens,
                ),
                budget_cents=budget_cents,
            )
            trace.record_llm_call()
            trace.record_outputs({"plan_preview": plan_resp.text[:200]})

        with trace.step("synthesise", inputs_summary={"slug": slug}):
            synth_resp = run_with_fallback(
                CompletionRequest(
                    model="",
                    messages=[
                        Message(role="system", content=synth_system),
                        Message(role="user",
                                content=synth_user_builder(plan_resp.text)),
                    ],
                    temperature=0.1, max_tokens=synth_max_tokens,
                ),
                budget_cents=budget_cents,
            )
            trace.record_llm_call()
            trace.record_outputs({"synth_preview": synth_resp.text[:200]})
    except (BudgetExceededError, LLMError) as exc:
        return _err(
            f"{slug}.llm_error",
            str(exc),
            {"trace": _safe_trace(trace)},
        )

    payload: dict[str, Any] = {
        "plan": plan_resp.text,
        "synthesis": synth_resp.text,
        "trace": trace.to_dict(),
    }
    if extra_output:
        payload.update(extra_output)
    return _annotate(payload, llm_used=True)


def _safe_trace(trace: TraceRecorder) -> dict[str, Any]:
    """Best-effort trace serialisation that survives an open step."""
    try:
        return trace.to_dict()
    except Exception:
        return {"version": 1, "step_count": 0, "steps": [],
                "total_llm_calls": 0, "total_duration_ms": 0}
