"""
types.py — dataclasses for the parallel runner pool.

# OWNS: FanOutSpec, WorkerResult, AggregatedResult, FailurePolicy.
# NOT OWNS: execution backend (dispatch.py), orchestration (pool.py).
#
# All dataclasses are frozen so they survive cross-thread passing without
# defensive copies and so a reasoning agent can embed them in its trace
# without worry of in-place mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class FailurePolicy(str, Enum):
    """How the FanOutRunner reacts to per-worker failures.

    * fail_on_any        — any failed worker → AggregatedResult.status='failed'.
    * tolerate_minority  — fail only if > half the workers fail.
    * best_of_n          — succeed if at least one worker returns; aggregate
                           uses only the successful subset.
    """

    FAIL_ON_ANY = "fail_on_any"
    TOLERATE_MINORITY = "tolerate_minority"
    BEST_OF_N = "best_of_n"


class AggregatedStatus(str, Enum):
    """Terminal status of a fan-out call."""

    OK = "ok"
    PARTIAL = "partial"  # some workers failed but policy tolerated it
    FAILED = "failed"


@dataclass(frozen=True)
class WorkerResult:
    """One worker's outcome. ``output`` is None iff ``status='failed'``."""

    worker_index: int
    status: str  # "ok" | "failed"
    output: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class FanOutSpec:
    """Input to FanOutRunner.run.

    ``worker_fn`` is invoked once per item in ``payloads``. The runner
    captures the return value as WorkerResult.output (must be a dict for
    workspace serialisation parity) and exceptions as WorkerResult.error.

    ``aggregator_fn`` runs once on the list of WorkerResult after every
    worker is terminal. It returns the final aggregated dict shown to the
    caller.

    ``workspace_id`` is optional; when provided, each worker's output is
    written to ``outputs/worker_{i}.json`` in the workspace so a reasoning
    agent can later inspect intermediate state.

    Why frozen + Callable: agents construct the spec once and the runner
    passes it through every step; in-place mutation would create races.
    """

    worker_fn: Callable[[dict[str, Any]], dict[str, Any]]
    payloads: list[dict[str, Any]]
    aggregator_fn: Callable[[list[WorkerResult]], dict[str, Any]]
    failure_policy: FailurePolicy = FailurePolicy.FAIL_ON_ANY
    max_concurrency: int = 8
    per_worker_timeout_seconds: int = 60
    workspace_id: str | None = None


@dataclass(frozen=True)
class AggregatedResult:
    """Output of FanOutRunner.run.

    ``output`` is whatever the aggregator returned; absent on hard failure.
    ``worker_results`` is the per-worker trace, always present so the
    reasoning agent can include it in its receipt.
    """

    status: AggregatedStatus
    worker_results: list[WorkerResult]
    output: dict[str, Any] | None = None
    error: str | None = None
    workspace_id: str | None = None
    total_duration_ms: int = 0
