"""
load_tester.py — Run a real HTTP load test against a URL and return latency statistics.

# OWNS: time-based HTTP load testing (rps × duration_seconds model)
# NOT OWNS: per-request functional assertions (use live_endpoint_tester.py for that),
#           DNS/SSL inspection (use dns_inspector.py), security scanning
# INVARIANTS:
#   - All outbound URLs pass validate_outbound_url before any network I/O
#   - Hard limits on rps (≤ 50), duration (≤ 30 s), concurrency (≤ 20) are never
#     overridden by caller input — they are enforced before the test runs
#   - results list is only written under results_lock (thread-safety invariant)
# DECISIONS:
#   - threading + stop_event chosen over ThreadPoolExecutor so the run can be
#     stopped by time rather than by a pre-counted future list
#   - requests library used (already in requirements.txt) rather than httpx/aiohttp
#   - p-values computed from a fully sorted copy so they are exact, not estimated
# KNOWN DEBT:
#   - Rate limiting (rps enforcement) is approximate: workers spin freely and
#     only the total count reflects the achieved throughput. A token-bucket per
#     thread would enforce rps more precisely.

Input:
  url              (str, required)  — target URL
  rps              (int, default 5, max 50)   — target requests per second
  duration_seconds (int, default 10, max 30)  — how long to run the test
  concurrency      (int, default 5, max 20)   — parallel worker threads
  method           (str, default "GET")       — GET|POST|PUT|DELETE
  headers          (dict)                     — extra HTTP headers
  body             (str)                      — request body for POST/PUT
  expected_status  (int, default 200)         — status code counted as success

Output (success):
  {url, method, total_requests, success_count, error_count, error_rate,
   duration_actual_ms, throughput_rps, latency_ms{p50,p75,p95,p99,mean,min,max,std_dev},
   status_codes, errors, histogram, summary}

Output (error):
  {"error": {"code": "load_tester.<reason>", "message": "..."}}
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections import Counter
from typing import Any

import requests as _requests

from core.url_security import validate_outbound_url
from agents._contracts import agent_error as _err

# ---------------------------------------------------------------------------
# Hard limits — never changed by caller input
# ---------------------------------------------------------------------------
_MAX_RPS = 50
_MAX_DURATION_S = 30
_MAX_CONCURRENCY = 20
_REQUEST_TIMEOUT_S = 10

# Histogram bucket upper bounds in milliseconds; last bucket is open-ended (∞)
_HISTOGRAM_BUCKETS_MS = [1, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000]

_ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



def _percentile(sorted_data: list[float], p: float) -> float:
    """Return the p-th percentile from a pre-sorted list."""
    if not sorted_data:
        return 0.0
    idx = int(math.ceil(len(sorted_data) * p / 100.0)) - 1
    return round(sorted_data[max(0, min(idx, len(sorted_data) - 1))], 2)


def _build_histogram(latencies_ms: list[float]) -> list[dict[str, Any]]:
    """Return latency distribution across fixed bucket boundaries."""
    buckets: list[dict[str, Any]] = []
    prev = 0
    for upper in _HISTOGRAM_BUCKETS_MS:
        count = sum(1 for v in latencies_ms if prev <= v < upper)
        buckets.append({"bucket_ms": upper, "count": count})
        prev = upper
    # Open-ended final bucket (≥ last boundary)
    buckets.append(
        {
            "bucket_ms": None,  # None signals "∞"
            "count": sum(1 for v in latencies_ms if v >= _HISTOGRAM_BUCKETS_MS[-1]),
        }
    )
    return buckets


# ---------------------------------------------------------------------------
# Core load test runner
# ---------------------------------------------------------------------------

# Each record stored in `results`: (latency_ms: float, status_code: int|0, error: str|None)
_Result = tuple[float, int, str | None]


def _run_load_test(
    url: str,
    method: str,
    headers: dict,
    body: str | None,
    expected_status: int,
    duration_s: int,
    concurrency: int,
) -> list[_Result]:
    """Spawn `concurrency` worker threads; each fires requests until stop_event is set."""
    results: list[_Result] = []
    results_lock = threading.Lock()
    stop_event = threading.Event()

    session = _requests.Session()
    session.headers.update({"User-Agent": "aztea-load-tester/1.0"})

    def _worker() -> None:
        while not stop_event.is_set():
            start = time.monotonic()
            try:
                resp = session.request(
                    method,
                    url,
                    headers=headers,
                    data=body or None,
                    timeout=_REQUEST_TIMEOUT_S,
                    allow_redirects=False,
                )
                latency_ms = (time.monotonic() - start) * 1000
                error: str | None = None
                status = resp.status_code
            except _requests.exceptions.Timeout:
                latency_ms = (time.monotonic() - start) * 1000
                status = 0
                error = "timeout"
            except _requests.RequestException as exc:
                latency_ms = (time.monotonic() - start) * 1000
                status = 0
                error = f"{type(exc).__name__}: {str(exc)[:80]}"
            with results_lock:
                results.append((latency_ms, status, error))

    threads = [
        threading.Thread(target=_worker, daemon=True) for _ in range(concurrency)
    ]
    for t in threads:
        t.start()

    time.sleep(duration_s)
    stop_event.set()

    for t in threads:
        t.join(timeout=5)

    session.close()
    return results


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a real HTTP load test and return latency percentiles + error rates.

    Required: ``url``.
    Optional: ``rps`` (default 5), ``duration_seconds`` (default 10),
    ``concurrency`` (default 5), ``method`` (default GET), ``headers``,
    ``body``, ``expected_status`` (default 200).

    Returns ``{url, method, total_requests, success_count, error_count,
    error_rate, duration_actual_ms, throughput_rps, latency_ms, status_codes,
    errors, histogram, summary}``.
    """
    # --- URL validation ---
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("load_tester.missing_url", "url is required.")
    try:
        url = validate_outbound_url(raw_url, "url")
    except ValueError as exc:
        return _err("load_tester.invalid_url", str(exc))

    # --- Method ---
    method = str(payload.get("method") or "GET").strip().upper()
    if method not in _ALLOWED_METHODS:
        return _err(
            "load_tester.invalid_method",
            f"method must be one of {sorted(_ALLOWED_METHODS)}; got '{method}'.",
        )

    # --- Numeric limits ---
    try:
        rps = int(payload.get("rps") or 5)
        duration_s = int(payload.get("duration_seconds") or 10)
        concurrency = int(payload.get("concurrency") or 5)
        expected_status = int(payload.get("expected_status") or 200)
    except (TypeError, ValueError) as exc:
        return _err(
            "load_tester.invalid_parameter",
            f"rps, duration_seconds, concurrency, and expected_status must be integers: {exc}",
        )

    limit_errors = []
    if rps < 1 or rps > _MAX_RPS:
        limit_errors.append(f"rps must be between 1 and {_MAX_RPS}; got {rps}")
    if duration_s < 1 or duration_s > _MAX_DURATION_S:
        limit_errors.append(
            f"duration_seconds must be between 1 and {_MAX_DURATION_S}; got {duration_s}"
        )
    if concurrency < 1 or concurrency > _MAX_CONCURRENCY:
        limit_errors.append(
            f"concurrency must be between 1 and {_MAX_CONCURRENCY}; got {concurrency}"
        )
    if limit_errors:
        return _err("load_tester.limits_exceeded", "; ".join(limit_errors))

    # --- Headers ---
    headers: dict[str, str] = {}
    raw_headers = payload.get("headers")
    if raw_headers is not None:
        if not isinstance(raw_headers, dict):
            return _err("load_tester.invalid_headers", "headers must be an object.")
        headers = {str(k): str(v) for k, v in raw_headers.items()}

    # --- Body ---
    body: str | None = None
    raw_body = payload.get("body")
    if raw_body is not None:
        body = str(raw_body)

    # --- Run ---
    test_start = time.monotonic()
    raw_results = _run_load_test(
        url=url,
        method=method,
        headers=headers,
        body=body,
        expected_status=expected_status,
        duration_s=duration_s,
        concurrency=concurrency,
    )
    duration_actual_ms = int((time.monotonic() - test_start) * 1000)

    if not raw_results:
        return _err(
            "load_tester.unreachable",
            "No responses were recorded. The target may be unreachable.",
        )

    # --- Aggregate ---
    total_requests = len(raw_results)
    latencies_ms = [r[0] for r in raw_results]
    status_counts: Counter[str] = Counter(
        str(r[1]) for r in raw_results if r[1] != 0
    )
    # Status 0 = transport error — count separately
    transport_errors = sum(1 for r in raw_results if r[1] == 0)
    if transport_errors:
        status_counts["0"] = transport_errors

    success_count = sum(
        1 for r in raw_results if r[1] == expected_status
    )
    error_count = total_requests - success_count
    error_rate = round(error_count / total_requests, 4) if total_requests else 0.0

    throughput_rps = round(
        total_requests / (duration_actual_ms / 1000), 2
    ) if duration_actual_ms else 0.0

    # Unique error strings (max 10)
    seen_errors: list[str] = []
    seen_set: set[str] = set()
    for _, _, err_msg in raw_results:
        if err_msg and err_msg not in seen_set:
            seen_set.add(err_msg)
            seen_errors.append(err_msg)
            if len(seen_errors) >= 10:
                break

    sorted_latencies = sorted(latencies_ms)
    p50 = _percentile(sorted_latencies, 50)
    p75 = _percentile(sorted_latencies, 75)
    p95 = _percentile(sorted_latencies, 95)
    p99 = _percentile(sorted_latencies, 99)
    lat_mean = round(statistics.fmean(latencies_ms), 2) if latencies_ms else 0.0
    lat_min = round(min(latencies_ms), 2) if latencies_ms else 0.0
    lat_max = round(max(latencies_ms), 2) if latencies_ms else 0.0
    lat_std_dev = (
        round(statistics.stdev(latencies_ms), 2) if len(latencies_ms) > 1 else 0.0
    )

    error_pct = round(error_rate * 100, 1)
    summary = (
        f"p50={p50}ms p95={p95}ms p99={p99}ms "
        f"throughput={throughput_rps}rps "
        f"errors={error_pct}% "
        f"on {total_requests} requests"
    )

    return {
        "url": url,
        "method": method,
        "total_requests": total_requests,
        "success_count": success_count,
        "error_count": error_count,
        "error_rate": error_rate,
        "duration_actual_ms": duration_actual_ms,
        "throughput_rps": throughput_rps,
        "latency_ms": {
            "p50": p50,
            "p75": p75,
            "p95": p95,
            "p99": p99,
            "mean": lat_mean,
            "min": lat_min,
            "max": lat_max,
            "std_dev": lat_std_dev,
        },
        "status_codes": dict(status_counts),
        "errors": seen_errors,
        "histogram": _build_histogram(latencies_ms),
        "summary": summary,
    }
