"""
pool.py — FanOutRunner: spawn N workers in parallel, aggregate results.

# OWNS: FanOutRunner.run orchestration + failure-policy interpretation.
# NOT OWNS: per-worker execution (dispatch.py), shared-state storage
#           (core/workspaces.py).
#
# INVARIANTS:
#   * worker_fn is invoked at most once per payload.
#   * The returned AggregatedResult always contains the full per-worker trace,
#     even when status='failed' — reasoning agents must be able to see what
#     succeeded.
#   * Workspace writes are best-effort: a workspace write failure logs but
#     does not fail the overall run. The reasoning agent's primary value is
#     the aggregated output; the workspace is for post-hoc audit only.
#   * max_concurrency is bounded between 1 and _MAX_FAN_OUT.
"""

from __future__ import annotations

import concurrent.futures as _futures
import json
import logging
import time
from typing import Any

from core.runners.dispatch import default_backend
from core.runners.types import (
    AggregatedResult,
    AggregatedStatus,
    FailurePolicy,
    FanOutSpec,
    WorkerResult,
)

_LOG = logging.getLogger(__name__)

# Hard cap on per-call fan-out. Higher requests are almost certainly bugs
# (typoed range, runaway loop) and would saturate the worker thread pool.
# Bump via env later if real consumers need it.
_MAX_FAN_OUT: int = 100


class FanOutRunner:
    """Orchestrate a parallel fan-out + aggregation.

    Usage:

        spec = FanOutSpec(
            worker_fn=lambda p: {"squared": p["n"] ** 2},
            payloads=[{"n": i} for i in range(16)],
            aggregator_fn=lambda results: {
                "sum_of_squares": sum(r.output["squared"] for r in results if r.output),
            },
            failure_policy=FailurePolicy.TOLERATE_MINORITY,
            max_concurrency=8,
        )
        result = FanOutRunner().run(spec)
        assert result.status == AggregatedStatus.OK
    """

    def __init__(self, backend=None) -> None:
        """``backend`` overrides the InProcessBackend default for tests / future
        backends. Production paths leave it None."""
        self._backend = backend or default_backend()

    def run(self, spec: FanOutSpec) -> AggregatedResult:
        """Execute spec.worker_fn in parallel and aggregate. Always returns."""
        _validate_spec(spec)
        start = time.perf_counter()
        worker_results = self._run_workers(spec)
        total_duration_ms = int((time.perf_counter() - start) * 1000)

        if spec.workspace_id:
            self._persist_to_workspace(spec.workspace_id, worker_results)

        status = _compute_status(worker_results, spec.failure_policy)
        if status == AggregatedStatus.FAILED:
            return AggregatedResult(
                status=status,
                worker_results=worker_results,
                error=_summarise_failures(worker_results),
                workspace_id=spec.workspace_id,
                total_duration_ms=total_duration_ms,
            )

        try:
            aggregate = spec.aggregator_fn(worker_results)
        except Exception as exc:
            return AggregatedResult(
                status=AggregatedStatus.FAILED,
                worker_results=worker_results,
                error=f"aggregator_fn raised: {type(exc).__name__}: {exc}",
                workspace_id=spec.workspace_id,
                total_duration_ms=total_duration_ms,
            )

        if not isinstance(aggregate, dict):
            return AggregatedResult(
                status=AggregatedStatus.FAILED,
                worker_results=worker_results,
                error=f"aggregator_fn must return dict, got {type(aggregate).__name__}",
                workspace_id=spec.workspace_id,
                total_duration_ms=total_duration_ms,
            )

        return AggregatedResult(
            status=status,
            worker_results=worker_results,
            output=aggregate,
            workspace_id=spec.workspace_id,
            total_duration_ms=total_duration_ms,
        )

    # ------------------------------------------------------------------
    # Worker dispatch
    # ------------------------------------------------------------------

    def _run_workers(self, spec: FanOutSpec) -> list[WorkerResult]:
        """Run every worker via the backend, preserving payload-index order."""
        max_workers = max(1, min(spec.max_concurrency, len(spec.payloads)))
        # Pre-size the results list so each future drops into its index slot
        # without needing a post-sort.
        results: list[WorkerResult | None] = [None] * len(spec.payloads)
        with _futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_to_idx = {
                ex.submit(
                    self._backend.run_one,
                    idx,
                    payload,
                    spec.worker_fn,
                    spec.per_worker_timeout_seconds,
                ): idx
                for idx, payload in enumerate(spec.payloads)
            }
            for future in _futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    # Backend contracts to never raise; defence-in-depth here.
                    results[idx] = WorkerResult(
                        worker_index=idx,
                        status="failed",
                        error=f"backend raised: {type(exc).__name__}: {exc}",
                    )
        # Replace any unexpected None (shouldn't happen with the contract).
        return [r if r is not None else WorkerResult(
            worker_index=i, status="failed", error="missing result",
        ) for i, r in enumerate(results)]

    # ------------------------------------------------------------------
    # Workspace persistence (best-effort)
    # ------------------------------------------------------------------

    def _persist_to_workspace(
        self, workspace_id: str, results: list[WorkerResult],
    ) -> None:
        """Write each worker's output as outputs/worker_{i}.json.

        Errors are logged but never propagated — workspace persistence is
        a post-hoc audit aid, not a critical path.
        """
        try:
            from core import workspaces as _ws
        except ImportError:
            _LOG.debug("core.workspaces unavailable; skipping persistence")
            return
        for r in results:
            body = {
                "worker_index": r.worker_index,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "output": r.output,
                "error": r.error,
            }
            try:
                _ws.write_artifact(
                    workspace_id,
                    f"outputs/worker_{r.worker_index}.json",
                    json.dumps(body, sort_keys=True).encode("utf-8"),
                    content_type="application/json",
                )
            except Exception as exc:
                _LOG.warning(
                    "workspace write for worker %d failed: %s",
                    r.worker_index, exc,
                )


# ---------------------------------------------------------------------------
# Module-level helpers (pure)
# ---------------------------------------------------------------------------


def _validate_spec(spec: FanOutSpec) -> None:
    """Pure: validate spec invariants. Raises ValueError on bad input."""
    if not callable(spec.worker_fn):
        raise ValueError("FanOutSpec.worker_fn must be callable")
    if not callable(spec.aggregator_fn):
        raise ValueError("FanOutSpec.aggregator_fn must be callable")
    if not isinstance(spec.payloads, list):
        raise ValueError("FanOutSpec.payloads must be a list")
    if not spec.payloads:
        raise ValueError("FanOutSpec.payloads must be non-empty")
    if len(spec.payloads) > _MAX_FAN_OUT:
        raise ValueError(
            f"FanOutSpec.payloads must have <= {_MAX_FAN_OUT} items, "
            f"got {len(spec.payloads)}"
        )
    for i, p in enumerate(spec.payloads):
        if not isinstance(p, dict):
            raise ValueError(f"FanOutSpec.payloads[{i}] must be dict, got {type(p).__name__}")
    if spec.max_concurrency < 1:
        raise ValueError(
            f"FanOutSpec.max_concurrency must be >= 1, got {spec.max_concurrency}"
        )
    if spec.per_worker_timeout_seconds < 1:
        raise ValueError(
            f"FanOutSpec.per_worker_timeout_seconds must be >= 1, "
            f"got {spec.per_worker_timeout_seconds}"
        )


def _compute_status(
    results: list[WorkerResult], policy: FailurePolicy,
) -> AggregatedStatus:
    """Pure: apply failure policy to derive the AggregatedStatus.

    Why isolated: keeping the policy logic out of the run() side-effect path
    means the contract is unit-testable without spinning up worker threads.
    """
    failed = [r for r in results if r.status != "ok"]
    succeeded = [r for r in results if r.status == "ok"]
    total = len(results)

    if not failed:
        return AggregatedStatus.OK
    if policy == FailurePolicy.FAIL_ON_ANY:
        return AggregatedStatus.FAILED
    if policy == FailurePolicy.TOLERATE_MINORITY:
        # Strict minority: fewer than half failed.
        return (
            AggregatedStatus.PARTIAL if len(failed) * 2 < total
            else AggregatedStatus.FAILED
        )
    if policy == FailurePolicy.BEST_OF_N:
        return (
            AggregatedStatus.PARTIAL if succeeded
            else AggregatedStatus.FAILED
        )
    # Defensive: unknown policy.
    return AggregatedStatus.FAILED


def _summarise_failures(results: list[WorkerResult]) -> str:
    """Pure: short human-readable summary of failed worker errors."""
    failed = [r for r in results if r.status != "ok"]
    if not failed:
        return ""
    first = failed[0]
    extras = len(failed) - 1
    suffix = f" (+ {extras} more)" if extras > 0 else ""
    return (
        f"{len(failed)}/{len(results)} workers failed; "
        f"first error: worker[{first.worker_index}] {first.error}{suffix}"
    )
