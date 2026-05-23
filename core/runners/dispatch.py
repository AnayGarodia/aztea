"""
dispatch.py — execution backends for the parallel runner pool.

# OWNS: backend interface + InProcessBackend (ThreadPoolExecutor) +
#       JobLifecycleBackend stub.
# NOT OWNS: orchestration (pool.py), failure policy (types.py).
#
# DECISIONS:
#   * v0 ships ONE working backend (InProcessBackend) used by reasoning
#     agents that fan out over IO-bound work — LLM calls in parallel, HTTP
#     fetches against many endpoints. It pays no payment-lifecycle cost
#     because the workers are pure callables inside the parent's hire.
#
#   * JobLifecycleBackend (cross-process child-jobs through core/jobs/)
#     is intentionally STUBBED. Honest gap: spawning real child jobs needs
#     per-payload pre_call_charge, agent ID resolution, and wallet plumbing
#     that only makes sense per-consumer. The first agent that needs it
#     (B8 Refactor Verifier) will wire its own resolver against this
#     interface; the stub documents the contract so the integration is
#     mechanical.
"""

from __future__ import annotations

import concurrent.futures as _futures
import logging
import threading
import time
from typing import Any, Callable, Protocol

from core.runners.types import WorkerResult

_LOG = logging.getLogger(__name__)


class _Backend(Protocol):
    """Interface that any runner-pool backend must satisfy."""

    name: str

    def run_one(
        self,
        worker_index: int,
        payload: dict[str, Any],
        worker_fn: Callable[[dict[str, Any]], dict[str, Any]],
        timeout_seconds: int,
    ) -> WorkerResult:
        """Run a single worker invocation. Must NOT raise — wrap errors into WorkerResult."""
        ...


# ---------------------------------------------------------------------------
# InProcessBackend — v0 default
# ---------------------------------------------------------------------------


class InProcessBackend:
    """Backend that runs each worker as a callable on a ThreadPoolExecutor.

    Suitable for IO-bound fan-out (LLM calls, HTTP fetches, DB reads). For
    CPU-bound parallelism the GIL bites; the JobLifecycleBackend's
    cross-process design solves that — once it's wired.
    """

    name = "in_process"

    def run_one(
        self,
        worker_index: int,
        payload: dict[str, Any],
        worker_fn: Callable[[dict[str, Any]], dict[str, Any]],
        timeout_seconds: int,
    ) -> WorkerResult:
        """Execute worker_fn(payload) on the calling thread.

        Why no spawning here: the calling thread IS one of the pool's
        threads (FanOutRunner uses ThreadPoolExecutor to call run_one
        concurrently). Wrapping with another executor is redundant and
        loses the timeout signal.
        """
        start = time.perf_counter()
        # The outer ThreadPoolExecutor doesn't enforce per-worker timeouts,
        # so we enforce it here via a one-shot inner thread. Simpler than
        # signals (signal-based timeouts only work on the main thread on
        # POSIX).
        result: dict[str, Any] = {}
        error_holder: dict[str, str] = {}

        def _target():
            try:
                out = worker_fn(payload)
            except Exception as exc:
                error_holder["err"] = f"{type(exc).__name__}: {exc}"
                return
            if not isinstance(out, dict):
                error_holder["err"] = (
                    f"worker_fn must return dict, got {type(out).__name__}"
                )
                return
            result["out"] = out

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=timeout_seconds)
        duration_ms = int((time.perf_counter() - start) * 1000)

        if thread.is_alive():
            # The thread is daemon so it won't block process exit, but
            # we can't actually kill it — Python doesn't support that.
            # Log so an operator can chase a runaway worker_fn.
            _LOG.warning(
                "runner worker %d exceeded %ds timeout (thread leaked)",
                worker_index, timeout_seconds,
            )
            return WorkerResult(
                worker_index=worker_index,
                status="failed",
                error=f"timeout after {timeout_seconds}s",
                duration_ms=duration_ms,
            )
        if "err" in error_holder:
            return WorkerResult(
                worker_index=worker_index,
                status="failed",
                error=error_holder["err"],
                duration_ms=duration_ms,
            )
        return WorkerResult(
            worker_index=worker_index,
            status="ok",
            output=result.get("out", {}),
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# JobLifecycleBackend — v0.2 placeholder
# ---------------------------------------------------------------------------


class JobLifecycleBackend:
    """Backend that spawns each worker as a real child job through core/jobs/.

    INTEGRATION CONTRACT (for the first consumer agent):

      To activate this backend, supply a `child_spec_resolver`: a callable
      `(payload) -> dict` that returns the kwargs for `core.jobs.crud.create_job`
      (agent_id, caller_owner_id, caller_wallet_id, agent_wallet_id,
       platform_wallet_id, price_cents, charge_tx_id, input_payload, ...).

      The resolver is responsible for:
        * Calling `core.payments.base.pre_call_charge` to debit the caller.
        * Picking the correct child agent_id and wallet ids.
        * Setting `parent_job_id` to the runner's parent_job_id.

      The backend then:
        * Calls `create_job(**resolved_kwargs)` for each payload.
        * Polls `list_child_jobs(parent_job_id, statuses=('complete','failed'))`
          until every spawned child reaches a terminal status (capped at
          per_worker_timeout_seconds * len(payloads)).
        * Reads each child's output from the shared workspace.
        * Returns WorkerResult per child.

    Why stubbed in v0: each consumer agent has different wallet plumbing
    (some use a single shared agent, some fan out to mixed agents); the
    resolver is the agent-specific seam. The first agent (B8 Refactor
    Verifier) will land both the consumer code and the resolver in the
    same PR so they ship coherently. The interface above is the contract
    for that PR.
    """

    name = "job_lifecycle"

    def __init__(
        self,
        parent_job_id: str,
        child_spec_resolver: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._parent_job_id = parent_job_id
        self._resolver = child_spec_resolver

    def run_one(
        self,
        worker_index: int,
        payload: dict[str, Any],
        worker_fn: Callable[[dict[str, Any]], dict[str, Any]],
        timeout_seconds: int,
    ) -> WorkerResult:
        # Intentional stub — see class docstring for the integration contract.
        raise NotImplementedError(
            "JobLifecycleBackend is stubbed in v0. The first consumer agent "
            "(B8 Refactor Verifier) lands the implementation together with "
            "its wallet/agent plumbing. See class docstring for the contract."
        )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def default_backend() -> _Backend:
    """Return the InProcessBackend singleton used by v0 reasoning agents."""
    return _DEFAULT_BACKEND


_DEFAULT_BACKEND: _Backend = InProcessBackend()
