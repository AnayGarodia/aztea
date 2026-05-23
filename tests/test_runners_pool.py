"""Tests for core/runners — parallel fan-out runner pool."""

from __future__ import annotations

import time

import pytest

from core.runners import (
    AggregatedResult,
    AggregatedStatus,
    FailurePolicy,
    FanOutRunner,
    FanOutSpec,
    InProcessBackend,
    JobLifecycleBackend,
    WorkerResult,
)
from core.runners.pool import (
    _compute_status,
    _summarise_failures,
    _validate_spec,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _result(idx: int, status: str, output=None, error=None) -> WorkerResult:
    return WorkerResult(
        worker_index=idx, status=status, output=output, error=error,
    )


def test_compute_status_all_ok():
    rs = [_result(0, "ok", {}), _result(1, "ok", {})]
    assert _compute_status(rs, FailurePolicy.FAIL_ON_ANY) == AggregatedStatus.OK
    assert _compute_status(rs, FailurePolicy.TOLERATE_MINORITY) == AggregatedStatus.OK
    assert _compute_status(rs, FailurePolicy.BEST_OF_N) == AggregatedStatus.OK


def test_compute_status_fail_on_any():
    rs = [_result(0, "ok", {}), _result(1, "failed", error="bad")]
    assert _compute_status(rs, FailurePolicy.FAIL_ON_ANY) == AggregatedStatus.FAILED


def test_compute_status_tolerate_minority():
    """Minority failure → partial; majority failure → failed."""
    # 1 of 4 fail → minority → partial
    rs1 = [_result(0, "failed"), _result(1, "ok", {}),
           _result(2, "ok", {}), _result(3, "ok", {})]
    assert _compute_status(rs1, FailurePolicy.TOLERATE_MINORITY) == AggregatedStatus.PARTIAL
    # 2 of 4 fail → not strict minority → failed
    rs2 = [_result(0, "failed"), _result(1, "failed"),
           _result(2, "ok", {}), _result(3, "ok", {})]
    assert _compute_status(rs2, FailurePolicy.TOLERATE_MINORITY) == AggregatedStatus.FAILED


def test_compute_status_best_of_n():
    # At least one success → partial; zero successes → failed.
    rs1 = [_result(0, "failed"), _result(1, "failed"), _result(2, "ok", {})]
    assert _compute_status(rs1, FailurePolicy.BEST_OF_N) == AggregatedStatus.PARTIAL
    rs2 = [_result(0, "failed"), _result(1, "failed")]
    assert _compute_status(rs2, FailurePolicy.BEST_OF_N) == AggregatedStatus.FAILED


def test_summarise_failures_includes_first_error_and_count():
    rs = [_result(0, "failed", error="boom"), _result(1, "failed", error="zap"),
          _result(2, "ok", {})]
    summary = _summarise_failures(rs)
    assert "2/3" in summary
    assert "worker[0]" in summary
    assert "boom" in summary
    assert "+ 1 more" in summary


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------


def _trivial_spec(**overrides) -> FanOutSpec:
    defaults = dict(
        worker_fn=lambda p: {"echo": p},
        payloads=[{"i": 0}],
        aggregator_fn=lambda rs: {"count": len(rs)},
    )
    defaults.update(overrides)
    return FanOutSpec(**defaults)


def test_validate_spec_passes_minimal():
    _validate_spec(_trivial_spec())  # must not raise


def test_validate_spec_rejects_empty_payloads():
    with pytest.raises(ValueError, match="non-empty"):
        _validate_spec(_trivial_spec(payloads=[]))


def test_validate_spec_rejects_too_many_payloads():
    with pytest.raises(ValueError, match="<= 100"):
        _validate_spec(_trivial_spec(payloads=[{} for _ in range(101)]))


def test_validate_spec_rejects_non_dict_payload():
    with pytest.raises(ValueError, match="must be dict"):
        _validate_spec(_trivial_spec(payloads=["not-a-dict"]))


def test_validate_spec_rejects_non_callable_aggregator():
    with pytest.raises(ValueError, match="callable"):
        _validate_spec(_trivial_spec(aggregator_fn="not-callable"))


def test_validate_spec_rejects_bad_concurrency():
    with pytest.raises(ValueError, match="max_concurrency"):
        _validate_spec(_trivial_spec(max_concurrency=0))


# ---------------------------------------------------------------------------
# End-to-end: InProcessBackend
# ---------------------------------------------------------------------------


def test_run_fans_out_8_workers_and_aggregates_sum():
    spec = FanOutSpec(
        worker_fn=lambda p: {"squared": p["n"] ** 2},
        payloads=[{"n": i} for i in range(8)],
        aggregator_fn=lambda results: {
            "sum_of_squares": sum(
                r.output["squared"] for r in results if r.output
            ),
        },
        max_concurrency=4,
    )
    result = FanOutRunner().run(spec)
    assert result.status == AggregatedStatus.OK
    assert result.output == {"sum_of_squares": sum(i * i for i in range(8))}
    assert len(result.worker_results) == 8
    assert all(r.status == "ok" for r in result.worker_results)


def test_results_preserve_payload_order():
    """Even though execution is concurrent, worker_results[i] must correspond
    to payloads[i] so the aggregator can rely on ordering."""
    spec = FanOutSpec(
        worker_fn=lambda p: {"idx": p["i"]},
        payloads=[{"i": i} for i in range(10)],
        aggregator_fn=lambda rs: {"order": [r.output["idx"] for r in rs if r.output]},
        max_concurrency=10,
    )
    result = FanOutRunner().run(spec)
    assert result.output == {"order": list(range(10))}


def test_failing_worker_fail_on_any_policy():
    def fail_on_three(p):
        if p["i"] == 3:
            raise RuntimeError("worker 3 boom")
        return {"i": p["i"]}

    spec = FanOutSpec(
        worker_fn=fail_on_three,
        payloads=[{"i": i} for i in range(5)],
        aggregator_fn=lambda rs: {"n": len(rs)},
        failure_policy=FailurePolicy.FAIL_ON_ANY,
    )
    result = FanOutRunner().run(spec)
    assert result.status == AggregatedStatus.FAILED
    assert result.output is None
    assert "worker 3 boom" in (result.error or "")
    # Worker results still report what each worker did.
    assert result.worker_results[3].status == "failed"
    assert result.worker_results[0].status == "ok"


def test_failing_worker_tolerate_minority_policy():
    def fail_on_one(p):
        if p["i"] == 1:
            raise RuntimeError("only worker 1")
        return {"i": p["i"]}

    spec = FanOutSpec(
        worker_fn=fail_on_one,
        payloads=[{"i": i} for i in range(5)],
        aggregator_fn=lambda rs: {
            "ok_count": sum(1 for r in rs if r.status == "ok"),
        },
        failure_policy=FailurePolicy.TOLERATE_MINORITY,
    )
    result = FanOutRunner().run(spec)
    assert result.status == AggregatedStatus.PARTIAL
    assert result.output == {"ok_count": 4}


def test_failing_worker_best_of_n_policy():
    def fail_unless_zero(p):
        if p["i"] != 0:
            raise RuntimeError("nope")
        return {"i": 0}

    spec = FanOutSpec(
        worker_fn=fail_unless_zero,
        payloads=[{"i": i} for i in range(5)],
        aggregator_fn=lambda rs: {
            "wins": [r.output for r in rs if r.status == "ok"],
        },
        failure_policy=FailurePolicy.BEST_OF_N,
    )
    result = FanOutRunner().run(spec)
    assert result.status == AggregatedStatus.PARTIAL
    assert result.output == {"wins": [{"i": 0}]}


def test_aggregator_failure_marks_run_failed():
    spec = FanOutSpec(
        worker_fn=lambda p: {"x": p["i"]},
        payloads=[{"i": 0}],
        aggregator_fn=lambda rs: (_ for _ in ()).throw(ValueError("agg boom")),
    )
    result = FanOutRunner().run(spec)
    assert result.status == AggregatedStatus.FAILED
    assert "aggregator_fn raised" in result.error
    assert "agg boom" in result.error


def test_aggregator_must_return_dict():
    spec = FanOutSpec(
        worker_fn=lambda p: {"x": 1},
        payloads=[{"i": 0}],
        aggregator_fn=lambda rs: "not-a-dict",
    )
    result = FanOutRunner().run(spec)
    assert result.status == AggregatedStatus.FAILED
    assert "must return dict" in result.error


def test_worker_fn_must_return_dict():
    spec = FanOutSpec(
        worker_fn=lambda p: "string-not-dict",
        payloads=[{"i": 0}],
        aggregator_fn=lambda rs: {"n": len(rs)},
        failure_policy=FailurePolicy.BEST_OF_N,
    )
    result = FanOutRunner().run(spec)
    # All workers fail because they return non-dict; best_of_n then has no
    # successes, so the run is FAILED.
    assert result.status == AggregatedStatus.FAILED
    assert result.worker_results[0].status == "failed"


def test_per_worker_timeout_marks_failure():
    """A worker that exceeds its timeout is marked failed without killing the run."""
    def slow_worker(p):
        time.sleep(2)
        return {"done": True}

    spec = FanOutSpec(
        worker_fn=slow_worker,
        payloads=[{"i": 0}],
        aggregator_fn=lambda rs: {"n": len(rs)},
        per_worker_timeout_seconds=1,
        failure_policy=FailurePolicy.BEST_OF_N,
    )
    result = FanOutRunner().run(spec)
    assert result.worker_results[0].status == "failed"
    assert "timeout" in (result.worker_results[0].error or "")


def test_run_records_total_duration():
    spec = FanOutSpec(
        worker_fn=lambda p: {"x": 1},
        payloads=[{"i": 0}],
        aggregator_fn=lambda rs: {"n": len(rs)},
    )
    result = FanOutRunner().run(spec)
    assert result.total_duration_ms >= 0


# ---------------------------------------------------------------------------
# JobLifecycleBackend stub
# ---------------------------------------------------------------------------


def test_job_lifecycle_backend_is_stubbed():
    """The cross-process backend documents what it needs from consumers; it
    must NOT silently no-op or partially execute."""
    backend = JobLifecycleBackend(
        parent_job_id="fake",
        child_spec_resolver=lambda p: {},
    )
    with pytest.raises(NotImplementedError, match="B8 Refactor Verifier"):
        backend.run_one(0, {"i": 0}, lambda p: {}, 60)


def test_default_backend_is_in_process():
    from core.runners.dispatch import default_backend, InProcessBackend
    assert isinstance(default_backend(), InProcessBackend)
