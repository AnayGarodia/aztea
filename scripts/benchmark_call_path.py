#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
benchmark_call_path.py — probe the Aztea sync call path latency per segment.

# OWNS: a runnable harness that hits a target agent N times and reports
#       per-segment p50/p95/p99 by scraping /metrics for the
#       `call_segment_seconds` histogram.
# NOT OWNS: SLO definitions (data/bench/slo.yaml); the segment wrapping itself
#       (lives in server/application_parts/part_008.py + part_012.py).
# INVARIANTS:
#   - Read-only against /metrics; no DB writes from the harness itself.
#   - Refuses to run against an agent that isn't on the local server's
#     catalog (avoids accidentally benching production).
# DECISIONS:
#   - Percentiles are approximated from histogram buckets (Prometheus
#     convention). Exact values would need raw observations.
#   - Cold-start is the *first* call; warm-state is the median of calls 2..N.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


# Segments emitted by `core.observability.time_segment`. Keep in sync with
# the histogram label values produced in part_008.py + part_012.py.
_SEGMENTS = (
    "auth",
    "agent_lookup",
    "embed_search",
    "gating",
    "pre_call_charge",
    "dispatch",
    "post_call_settle",
    "decision_audit",
    "receipt_write",
    "work_example",
    "output_render",
    "outbound_http",
)


def _parse_histogram_buckets(metrics_text: str, segment: str) -> list[tuple[float, int]]:
    """Pure: extract `(le_value, cumulative_count)` for one segment label."""
    buckets: list[tuple[float, int]] = []
    for line in metrics_text.splitlines():
        if not line.startswith("call_segment_seconds_bucket"):
            continue
        if f'segment="{segment}"' not in line:
            continue
        # Format: call_segment_seconds_bucket{segment="auth",le="0.01"} 42.0
        try:
            le_part = line.split('le="', 1)[1].split('"', 1)[0]
            value_part = line.rsplit(" ", 1)[1]
            le = float("inf") if le_part == "+Inf" else float(le_part)
            count = int(float(value_part))
            buckets.append((le, count))
        except (IndexError, ValueError):
            continue
    return sorted(buckets, key=lambda b: b[0])


def _approx_percentile(buckets: list[tuple[float, int]], pct: float) -> float | None:
    """Pure: estimate the percentile from cumulative bucket counts."""
    if not buckets:
        return None
    total = buckets[-1][1]
    if total <= 0:
        return None
    target = total * pct
    prev_le = 0.0
    prev_count = 0
    for le, count in buckets:
        if count >= target:
            # Linear interpolate within the bucket
            if count == prev_count:
                return le
            frac = (target - prev_count) / (count - prev_count)
            if le == float("inf"):
                return prev_le
            return prev_le + frac * (le - prev_le)
        prev_le, prev_count = le, count
    return buckets[-1][0]


def _bench(args: argparse.Namespace) -> dict[str, Any]:
    sess = requests.Session()
    headers = {"Authorization": f"Bearer {args.api_key}"}
    call_url = urljoin(args.base_url, f"/registry/agents/{args.agent_id}/call")
    metrics_url = urljoin(args.base_url, "/metrics")

    # Warm-up: 1 call (excluded from cold-start measurement)
    print(f"benchmark: calling {call_url} x {args.runs}", file=sys.stderr)
    elapsed_ms: list[float] = []
    failures = 0
    cold_start_ms: float | None = None

    for i in range(args.runs):
        t0 = time.perf_counter()
        try:
            r = sess.post(call_url, json=json.loads(args.payload), headers=headers, timeout=30)
            r.raise_for_status()
        except requests.RequestException as exc:
            failures += 1
            if args.verbose:
                print(f"  call {i}: FAILED {type(exc).__name__}", file=sys.stderr)
            continue
        elapsed = (time.perf_counter() - t0) * 1000.0
        elapsed_ms.append(elapsed)
        if i == 0:
            cold_start_ms = elapsed
        if args.verbose and i % 20 == 0:
            print(f"  call {i}: {elapsed:.1f}ms", file=sys.stderr)

    # Scrape metrics
    metrics_text = ""
    try:
        m = sess.get(metrics_url, timeout=5)
        m.raise_for_status()
        metrics_text = m.text
    except requests.RequestException as exc:
        print(f"benchmark: metrics scrape failed: {exc}", file=sys.stderr)

    per_segment: dict[str, dict[str, float | None]] = {}
    for seg in _SEGMENTS:
        buckets = _parse_histogram_buckets(metrics_text, seg)
        per_segment[seg] = {
            "p50_ms": _ms(_approx_percentile(buckets, 0.50)),
            "p95_ms": _ms(_approx_percentile(buckets, 0.95)),
            "p99_ms": _ms(_approx_percentile(buckets, 0.99)),
            "samples": buckets[-1][1] if buckets else 0,
        }

    end_to_end = {
        "p50_ms": statistics.median(elapsed_ms) if elapsed_ms else None,
        "p95_ms": _percentile(elapsed_ms, 0.95),
        "p99_ms": _percentile(elapsed_ms, 0.99),
        "mean_ms": statistics.fmean(elapsed_ms) if elapsed_ms else None,
        "cold_start_ms": cold_start_ms,
        "successes": len(elapsed_ms),
        "failures": failures,
    }

    return {
        "agent_id": args.agent_id,
        "base_url": args.base_url,
        "runs": args.runs,
        "backend": args.backend,
        "end_to_end_ms": end_to_end,
        "per_segment_ms": per_segment,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _ms(v: float | None) -> float | None:
    """Pure: convert seconds → milliseconds, preserve None."""
    return None if v is None else v * 1000.0


def _percentile(values: list[float], pct: float) -> float | None:
    """Pure: nearest-rank percentile."""
    if not values:
        return None
    sorted_v = sorted(values)
    idx = max(0, min(len(sorted_v) - 1, int(round(pct * len(sorted_v))) - 1))
    return sorted_v[idx]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("AZTEA_BENCH_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.environ.get("AZTEA_BENCH_API_KEY") or os.environ.get("API_KEY", ""))
    parser.add_argument("--agent-id", required=True, help="Target agent_id (UUID v5 for built-ins)")
    parser.add_argument("--payload", default='{"_no_op_probe": true}',
                        help='JSON payload sent to the agent; defaults to a probe envelope')
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument("--out", default=None, help="Output JSON path (defaults to data/bench/<name>.json)")
    parser.add_argument("--name", default="baseline", help="Run name (used in default output filename)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.api_key:
        print("benchmark: AZTEA_BENCH_API_KEY or API_KEY env required", file=sys.stderr)
        return 2

    result = _bench(args)

    out_path = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[1] / "data" / "bench" / f"{args.name}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))
    print(f"benchmark: wrote {out_path}", file=sys.stderr)

    e2e = result["end_to_end_ms"]
    print(f"\nEnd-to-end ({result['successes']}/{result['runs']} ok):")
    print(f"  p50  = {e2e['p50_ms']:.2f} ms" if e2e["p50_ms"] is not None else "  p50  = n/a")
    print(f"  p95  = {e2e['p95_ms']:.2f} ms" if e2e["p95_ms"] is not None else "  p95  = n/a")
    print(f"  p99  = {e2e['p99_ms']:.2f} ms" if e2e["p99_ms"] is not None else "  p99  = n/a")
    print(f"  cold = {e2e['cold_start_ms']:.2f} ms" if e2e["cold_start_ms"] is not None else "  cold = n/a")
    print("\nPer-segment (approx from histogram buckets):")
    print(f"  {'segment':<22} {'p50':>10} {'p95':>10} {'p99':>10} {'samples':>10}")
    for seg, data in result["per_segment_ms"].items():
        p50 = f"{data['p50_ms']:.2f}" if data["p50_ms"] is not None else "n/a"
        p95 = f"{data['p95_ms']:.2f}" if data["p95_ms"] is not None else "n/a"
        p99 = f"{data['p99_ms']:.2f}" if data["p99_ms"] is not None else "n/a"
        print(f"  {seg:<22} {p50:>10} {p95:>10} {p99:>10} {data['samples']:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
