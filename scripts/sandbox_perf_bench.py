#!/usr/bin/env python3
"""sandbox_perf_bench — measure live_sandbox latency vs the spec's claims.

Usage:
    AZTEA_RUN_DOCKER_TESTS=1 python3 scripts/sandbox_perf_bench.py \
        [--repo https://github.com/<owner>/<node-pg-fixture>.git] \
        [--cycles 3]

Prints a table of measured latencies next to the demand-spec targets so
the gap (or lack thereof) is visible at a glance. Exit code is non-zero
if a measurement misses its target by more than 25% — useful for CI
regression catching but the default operator run is informational.

# OWNS: a deterministic benchmark of the core sandbox lifecycle:
#         cold boot, warm boot from fork, exec round-trip, snapshot,
#         restore. Runs against a real public Node+Postgres fixture.
# NOT OWNS: profiling individual code paths — that's sandbox_trace's
#           job. We measure outcomes, not the call tree.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

# Add repo root so the script can import core.sandbox without an install step.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# Demand-spec targets from the original live_sandbox proposal.
_TARGETS = {
    "cold_boot_seconds": 60.0,
    "warm_boot_seconds": 15.0,
    "exec_round_trip_seconds": 0.2,
    "snapshot_seconds": 5.0,
    "fork_seconds": 10.0,
}


def main() -> int:
    """Side-effect: drive the benchmark; print the table; return CI exit code."""
    args = _parse_args()
    if not _docker_available():
        print(
            "FAIL: Docker daemon unreachable. Start Docker Desktop or run "
            "`systemctl start docker` and retry.",
            file=sys.stderr,
        )
        return 2
    if os.environ.get("AZTEA_RUN_DOCKER_TESTS") != "1":
        print(
            "Refusing to run without AZTEA_RUN_DOCKER_TESTS=1 set; this "
            "benchmark boots real containers and consumes real resources."
        )
        return 2
    print(f"=== sandbox_perf_bench ({args.cycles} cycle(s)) ===")
    print(f"fixture: {args.repo}")
    print()
    samples: dict[str, list[float]] = {k: [] for k in _TARGETS}
    for i in range(args.cycles):
        print(f"  cycle {i + 1}/{args.cycles}")
        _run_cycle(args.repo, samples)
    print()
    return _print_report(samples)


def _run_cycle(repo: str, samples: dict[str, list[float]]) -> None:
    """Side-effect: one full cycle of boot → exec → snapshot → fork → stop."""
    from core.sandbox import dispatch

    # Cold boot
    t0 = time.time()
    start_resp = dispatch({
        "action": "sandbox_start",
        "input": {
            "source": {"kind": "git", "url": repo, "shallow": True},
            "boot": {"strategy": "auto"},
            "lifetime": {"max_minutes": 10},
            "network": {"egress": "isolated"},
        },
    })
    cold_t = time.time() - t0
    if "error" in start_resp:
        raise RuntimeError(f"start failed: {start_resp['error']}")
    samples["cold_boot_seconds"].append(cold_t)
    sandbox_id = start_resp["sandbox_id"]
    try:
        # Exec round-trip — measure the simplest possible command so we
        # isolate dispatch + docker-exec overhead from real work.
        t0 = time.time()
        out = dispatch({
            "action": "sandbox_exec",
            "input": {"sandbox_id": sandbox_id, "cmd": "true"},
        })
        samples["exec_round_trip_seconds"].append(time.time() - t0)
        assert "error" not in out, out
        # Snapshot
        t0 = time.time()
        snap = dispatch({
            "action": "sandbox_snapshot",
            "input": {"sandbox_id": sandbox_id},
        })
        samples["snapshot_seconds"].append(time.time() - t0)
        assert "error" not in snap, snap
        # Fork (warm boot from snapshot)
        t0 = time.time()
        forked = dispatch({
            "action": "sandbox_fork",
            "input": {
                "source_sandbox_id": sandbox_id,
                "snapshot_id": snap["snapshot_id"],
            },
        })
        samples["fork_seconds"].append(time.time() - t0)
        samples["warm_boot_seconds"].append(samples["fork_seconds"][-1])
        # Tear down the fork right away.
        if "sandbox_id" in forked:
            dispatch({"action": "sandbox_stop", "input": {"sandbox_id": forked["sandbox_id"]}})
    finally:
        dispatch({"action": "sandbox_stop", "input": {"sandbox_id": sandbox_id}})


def _print_report(samples: dict[str, list[float]]) -> int:
    """Pure-ish: format the perf table; return non-zero on a >25% miss."""
    print(f"{'metric':<28} {'target (spec)':>14} {'measured p50':>14} {'measured p95':>14} {'verdict':>10}")
    print("-" * 86)
    exit_code = 0
    for metric, target in _TARGETS.items():
        values = samples.get(metric) or []
        if not values:
            print(f"{metric:<28} {target:>14.2f} {'n/a':>14} {'n/a':>14} {'NO DATA':>10}")
            continue
        p50 = statistics.median(values)
        p95 = max(values) if len(values) < 20 else statistics.quantiles(values, n=20)[18]
        slack = 1.25 * target
        verdict = "PASS" if p95 <= target else (
            "MISS" if p95 <= slack else "FAIL"
        )
        if verdict == "FAIL":
            exit_code = 1
        print(f"{metric:<28} {target:>14.2f} {p50:>14.2f} {p95:>14.2f} {verdict:>10}")
    print()
    if exit_code != 0:
        print("FAIL: at least one metric exceeded its target by >25%.")
    else:
        print("OK: all metrics within 25% of target (p95).")
    return exit_code


def _docker_available() -> bool:
    """Side-effect: probe whether the docker daemon responds."""
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=5,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=os.environ.get(
            "AZTEA_BENCH_REPO_URL",
            "https://github.com/aztea/node-pg-fixture.git",
        ),
        help="public GitHub repo URL with docker-compose.yml + Node + Postgres",
    )
    parser.add_argument(
        "--cycles", type=int, default=int(os.environ.get("AZTEA_BENCH_CYCLES", "3")),
        help="number of full cycles to measure (default 3)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
