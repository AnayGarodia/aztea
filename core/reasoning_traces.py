"""
reasoning_traces.py — structured trace recorder for reasoning agents.

# OWNS: TraceRecorder context manager, Step dataclass, to_dict serialiser.
# NOT OWNS: agent output schema (each agent owns its top-level shape);
#           workspace storage of large traces (caller's responsibility).
#
# INVARIANTS:
#   * A trace contains an ordered list of completed steps.
#   * Each step records duration_ms, inputs_summary, outputs_summary, llm_calls.
#   * to_dict() is JSON-serialisable; safe to embed directly in agent output.
#   * The recorder is NOT thread-safe — one recorder per hire, called from
#     a single thread of reasoning. Fan-out agents create one recorder per
#     worker and aggregate the trace dicts at the parent.
#
# DECISIONS:
#   * Steps capture a *summary* of inputs/outputs, not full payloads. Agents
#     decide what's significant. This keeps traces auditable without
#     ballooning the receipt size or leaking large blobs into MCP responses.
#   * llm_calls is a simple counter incremented by the agent. Pairing it
#     with the budget_cents knob in core/llm/fallback.py gives the
#     receipt enough detail to confirm the reasoning loop ran (≥2 calls).
#   * Failed steps still appear in the trace with status='failed' and the
#     error message. Receipts must reveal what was tried, not only what
#     worked.

Why centralised: Section 6.3 of the strategy doc requires every reasoning
agent to return an auditable receipt. Letting agents invent their own
format means each renderer breaks. Centralising the schema here keeps the
contract single-sourced.

Example:
    from core.reasoning_traces import TraceRecorder

    trace = TraceRecorder()
    with trace.step("retrieve_similar_hunks", inputs_summary={"hunk_count": 3}):
        results = vs.top_k(...)
        trace.record_outputs({"hits": len(results)})
        trace.record_llm_call()
    with trace.step("synthesise_review", inputs_summary={"hit_count": len(results)}):
        review = llm_complete(...)
        trace.record_llm_call()
        trace.record_outputs({"review_length": len(review)})
    output["trace"] = trace.to_dict()
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generator

_LOG = logging.getLogger(__name__)

# Cap on individual summary size so a careless agent can't blow the receipt
# past sensible MCP response limits. Real artifacts go in a workspace; the
# trace only carries pointers.
_MAX_SUMMARY_CHARS = 4_096

# Cap on the number of steps in a single trace. A reasoning loop that runs
# more than 200 steps is doing something pathological — either a runaway
# loop or a missing abstraction. Surface it loudly.
_MAX_STEPS = 200


@dataclass
class Step:
    """One completed reasoning step. Created by TraceRecorder; not user-built."""

    name: str
    started_at: str
    duration_ms: int
    inputs_summary: dict[str, Any]
    outputs_summary: dict[str, Any]
    llm_calls: int
    status: str  # "ok" | "failed"
    error: str | None = None


@dataclass
class _ActiveStep:
    """Mutable scratch for an in-flight step. Promoted to Step on exit."""

    name: str
    started_at: str
    start_perf: float
    inputs_summary: dict[str, Any]
    outputs_summary: dict[str, Any] = field(default_factory=dict)
    llm_calls: int = 0


class TraceRecorder:
    """Ordered recorder for reasoning steps. One instance per hire.

    Why a class and not module-level state: reasoning agents are sometimes
    invoked re-entrantly (e.g. a parent agent that hires a sub-agent inside
    a step). A module-level recorder would conflate their steps. Per-hire
    instances stay clean.
    """

    def __init__(self) -> None:
        self._steps: list[Step] = []
        self._current: _ActiveStep | None = None

    @contextmanager
    def step(
        self,
        name: str,
        inputs_summary: dict[str, Any] | None = None,
    ) -> Generator["TraceRecorder", None, None]:
        """Open a reasoning step. Auto-closes on exit, capturing duration + status.

        Why context-manager: agents can't forget to close a step or attribute
        a duration to the wrong action. The with-block boundary is the step
        boundary.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("step name must be a non-empty string")
        if self._current is not None:
            raise RuntimeError(
                f"cannot open step '{name}' while step "
                f"'{self._current.name}' is still active"
            )
        if len(self._steps) >= _MAX_STEPS:
            raise RuntimeError(
                f"trace step cap ({_MAX_STEPS}) reached — runaway reasoning loop?"
            )

        active = _ActiveStep(
            name=name,
            started_at=_now_iso(),
            start_perf=time.perf_counter(),
            inputs_summary=_clip_summary(inputs_summary or {}),
        )
        self._current = active
        status = "ok"
        error: str | None = None
        try:
            yield self
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = int((time.perf_counter() - active.start_perf) * 1000)
            self._steps.append(
                Step(
                    name=active.name,
                    started_at=active.started_at,
                    duration_ms=duration_ms,
                    inputs_summary=active.inputs_summary,
                    outputs_summary=_clip_summary(active.outputs_summary),
                    llm_calls=active.llm_calls,
                    status=status,
                    error=error,
                )
            )
            self._current = None

    def record_outputs(self, summary: dict[str, Any]) -> None:
        """Attach an outputs summary to the active step. Replaces prior values.

        Why replace and not merge: agents typically build the summary near
        the end of a step from the function-local result; partial merges
        are a footgun.
        """
        if self._current is None:
            raise RuntimeError("record_outputs called outside a step()")
        if not isinstance(summary, dict):
            raise TypeError(f"summary must be a dict, got {type(summary).__name__}")
        self._current.outputs_summary = dict(summary)

    def record_llm_call(self, count: int = 1) -> None:
        """Increment the LLM-call counter for the active step."""
        if self._current is None:
            raise RuntimeError("record_llm_call called outside a step()")
        if not isinstance(count, int) or count < 1:
            raise ValueError(f"count must be a positive int, got {count!r}")
        self._current.llm_calls += count

    def total_llm_calls(self) -> int:
        """Sum of LLM calls across every completed step.

        Why exposed: Section 6.2 requires reasoning agents make ≥ 2 LLM
        calls. Agents can self-assert before returning the receipt.
        """
        return sum(s.llm_calls for s in self._steps)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the trace to a JSON-compatible dict for the receipt."""
        if self._current is not None:
            # Forgetting to close a step usually means an exception was
            # swallowed. Loud failure is better than a silently-truncated
            # trace embedded in a paid receipt.
            raise RuntimeError(
                f"to_dict called while step '{self._current.name}' is still active"
            )
        return {
            "version": 1,
            "step_count": len(self._steps),
            "total_llm_calls": self.total_llm_calls(),
            "total_duration_ms": sum(s.duration_ms for s in self._steps),
            "steps": [
                {
                    "name": s.name,
                    "started_at": s.started_at,
                    "duration_ms": s.duration_ms,
                    "status": s.status,
                    "llm_calls": s.llm_calls,
                    "inputs_summary": s.inputs_summary,
                    "outputs_summary": s.outputs_summary,
                    **({"error": s.error} if s.error else {}),
                }
                for s in self._steps
            ],
        }


def _now_iso() -> str:
    """Pure: UTC ISO8601 'Z'-suffixed string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Pure: bound each summary value's serialised size.

    Why: an agent that drops a 1 MB blob into an inputs_summary balloons
    the receipt past MCP limits. Clipping at the helper boundary keeps
    every agent's traces predictably sized without each one repeating
    the same defensive code.
    """
    if not isinstance(summary, dict):
        raise TypeError(f"summary must be a dict, got {type(summary).__name__}")
    out: dict[str, Any] = {}
    for key, value in summary.items():
        if not isinstance(key, str):
            raise TypeError(f"summary keys must be str, got {type(key).__name__}")
        serialised = _coerce_to_summary_value(value)
        if isinstance(serialised, str) and len(serialised) > _MAX_SUMMARY_CHARS:
            dropped = len(serialised) - _MAX_SUMMARY_CHARS
            serialised = f"{serialised[:_MAX_SUMMARY_CHARS]}…[+{dropped} chars]"
        out[key] = serialised
    return out


def _coerce_to_summary_value(value: Any) -> Any:
    """Pure: collapse complex values to a JSON-safe summary form.

    Why explicit coercion: receipts are read by humans and by external
    auditors. Letting an arbitrary object slip through the json.dumps in
    to_dict() at write time means a failure surfaces in the receipt
    delivery path instead of right here at record-time.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce_to_summary_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce_to_summary_value(v) for k, v in value.items()}
    return repr(value)
