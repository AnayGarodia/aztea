"""Concurrency and parallel-call safety tests.

# OWNS: verifying multiple parallel `run()` invocations don't cross-pollute
#        state (sys.modules, tempfiles, LLM observation flags) and produce
#        consistent verdicts.
# NOT OWNS: HTTP-layer concurrency (the dispatcher handles that; covered
#            elsewhere).
"""

from __future__ import annotations

import glob
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from agents.quant_patch_validator import run as validator_run


_EQ_REF = "def f(x): return x * 2\n"
_BAD_CAND = "def f(x): return x + 1\n"  # buggy


def test_ten_parallel_calls_same_input_consistent_verdicts():
    """Same payload run by 10 threads → all return the same verdict."""

    def call():
        return validator_run(
            {
                "reference_code": _EQ_REF,
                "candidate_code": _EQ_REF,
                "fuzz_budget": "quick",
                "fuzz_seconds": 2,
            }
        )["verdict"]

    with ThreadPoolExecutor(max_workers=10) as pool:
        verdicts = list(pool.map(lambda _: call(), range(10)))
    assert all(v == "equivalent" for v in verdicts), verdicts


def test_parallel_calls_different_inputs_dont_cross_pollinate():
    """5 threads, half with equivalent inputs / half with regression inputs.
    Each must return its own correct verdict."""
    cases = [
        ("eq", _EQ_REF, _EQ_REF, "equivalent"),
        ("bug", _EQ_REF, _BAD_CAND, "regressions_found"),
        ("eq", _EQ_REF, _EQ_REF, "equivalent"),
        ("bug", _EQ_REF, _BAD_CAND, "regressions_found"),
        ("eq", _EQ_REF, _EQ_REF, "equivalent"),
    ]

    def call(case):
        _label, ref, cand, expected = case
        out = validator_run(
            {
                "reference_code": ref,
                "candidate_code": cand,
                "fuzz_budget": "quick",
                "fuzz_seconds": 3,
            }
        )
        return out["verdict"], expected

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(call, cases))
    for actual, expected in results:
        assert actual == expected, f"got {actual}, expected {expected}"


def test_concurrent_workspace_writes_dont_collide(monkeypatch):
    """5 threads, each with its own _workspace_id, all writing simultaneously."""
    captured: list[str] = []
    lock = threading.Lock()

    def fake_write_artifact(ws_id, path, body, content_type, **kwargs):
        with lock:
            captured.append(f"{ws_id}/{path}")

    import core.workspaces as ws_mod
    monkeypatch.setattr(ws_mod, "write_artifact", fake_write_artifact)

    def call(i):
        return validator_run(
            {
                "reference_code": _EQ_REF,
                "candidate_code": _EQ_REF,
                "fuzz_budget": "quick",
                "fuzz_seconds": 2,
                "_workspace_id": f"ws_thread_{i}",
            }
        )["verdict"]

    with ThreadPoolExecutor(max_workers=5) as pool:
        verdicts = list(pool.map(call, range(5)))
    assert all(v == "equivalent" for v in verdicts)
    # Each thread should have written exactly one qpv/report.json artifact.
    expected_paths = {f"ws_thread_{i}/qpv/report.json" for i in range(5)}
    assert set(captured) == expected_paths


def test_sequential_coverage_tempfile_cleanup():
    """Sequential coverage-tracked calls leave no tempfiles behind.

    Why sequential rather than parallel: coverage.py owns a global
    `sys.settrace`. Two concurrent `coverage.Coverage` instances fight
    over the trace function and the second one's `analysis2` raises.
    This is a documented v1 limitation; for parallel use, leave
    `track_coverage=False`.
    """
    before = set(glob.glob(f"{tempfile.gettempdir()}/qpv_cand_*.py"))
    for _ in range(8):
        validator_run(
            {
                "reference_code": _EQ_REF,
                "candidate_code": _EQ_REF,
                "fuzz_budget": "quick",
                "fuzz_seconds": 1.5,
                "track_coverage": True,
            }
        )
    after = set(glob.glob(f"{tempfile.gettempdir()}/qpv_cand_*.py"))
    leaked = after - before
    assert not leaked, f"tempfile leak: {leaked}"


def test_sys_modules_pollution_under_concurrency_is_bounded():
    """Repeated calls add qpv_* entries to sys.modules. They should not
    grow without bound. (No coverage here to avoid the trace-fn collision
    documented in `test_sequential_coverage_tempfile_cleanup`.)"""
    before = len([k for k in sys.modules if k.startswith("qpv_")])

    def call(_):
        return validator_run(
            {
                "reference_code": _EQ_REF,
                "candidate_code": _EQ_REF,
                "fuzz_budget": "quick",
                "fuzz_seconds": 1.5,
            }
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(call, range(10)))

    after = len([k for k in sys.modules if k.startswith("qpv_")])
    # Without coverage, only `qpv_ref` / `qpv_cand` (free-standing modules)
    # are created — they aren't registered in sys.modules. So delta ~ 0.
    assert after - before < 40, (
        f"sys.modules pollution: {after - before} new qpv_* entries after 10 calls"
    )
