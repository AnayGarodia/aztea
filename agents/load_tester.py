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
# 2026-05-20: bumped concurrency 20 → 50 and duration 30 → 120s. The
# previous caps boxed the agent into "sanity check" territory; users
# explicitly asking for real load tests were getting an artificially
# small footprint. Wall-clock per call is still bounded — 120s × 50 reqs
# stays well inside the 240s outer ceiling enforced by the dispatcher.
_MAX_RPS = 100
_MAX_DURATION_S = 120
_MAX_CONCURRENCY = 50
_REQUEST_TIMEOUT_S = 10
_DEFAULT_RPS = 5
_DEFAULT_DURATION_S = 10
_DEFAULT_CONCURRENCY = 5
_DEFAULT_EXPECTED_STATUS = 200
_THREAD_JOIN_TIMEOUT_S = 5
_MAX_UNIQUE_ERRORS = 10
_REQUEST_ERR_PREVIEW_CHARS = 80

# Histogram bucket upper bounds (ms); last bucket is open-ended (∞).
_HISTOGRAM_BUCKETS_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000)

_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})


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


def _fire_one_request(
    session: _requests.Session, method: str, url: str,
    headers: dict, body: str | None,
) -> _Result:
    """Side-effect: fire one HTTP request; returns ``(latency_ms, status, error)``."""
    start = time.monotonic()
    try:
        resp = session.request(
            method, url, headers=headers, data=body or None,
            timeout=_REQUEST_TIMEOUT_S, allow_redirects=False,
        )
        return ((time.monotonic() - start) * 1000, resp.status_code, None)
    except _requests.exceptions.Timeout:
        return ((time.monotonic() - start) * 1000, 0, "timeout")
    except _requests.RequestException as exc:
        return (
            (time.monotonic() - start) * 1000, 0,
            f"{type(exc).__name__}: {str(exc)[:_REQUEST_ERR_PREVIEW_CHARS]}",
        )


def _make_worker(
    session: _requests.Session, method: str, url: str,
    headers: dict, body: str | None,
    results: list[_Result], lock: threading.Lock, stop_event: threading.Event,
):
    """Pure factory: build a worker function bound to the supplied state."""

    def _worker() -> None:
        while not stop_event.is_set():
            record = _fire_one_request(session, method, url, headers, body)
            with lock:
                results.append(record)

    return _worker


def _run_load_test(
    url: str,
    method: str,
    headers: dict,
    body: str | None,
    expected_status: int,
    duration_s: int,
    concurrency: int,
) -> list[_Result]:
    """Side-effect: spawn ``concurrency`` worker threads firing requests for ``duration_s`` seconds."""
    results: list[_Result] = []
    lock = threading.Lock()
    stop_event = threading.Event()
    session = _requests.Session()
    session.headers.update({"User-Agent": "aztea-load-tester/1.0"})
    worker = _make_worker(session, method, url, headers, body, results, lock, stop_event)
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    time.sleep(duration_s)
    stop_event.set()
    for t in threads:
        t.join(timeout=_THREAD_JOIN_TIMEOUT_S)
    session.close()
    return results


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def _validate_url(payload: dict[str, Any]) -> str | dict:
    """Pure-ish: enforce SSRF + non-empty url; returns validated string or error envelope."""
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("load_tester.missing_url", "url is required.")
    try:
        return validate_outbound_url(raw_url, "url")
    except ValueError as exc:
        return _err("load_tester.invalid_url", str(exc))


def _validate_method(payload: dict[str, Any]) -> str | dict:
    """Pure: ensure ``method`` is in the allowed set."""
    method = str(payload.get("method") or "GET").strip().upper()
    if method not in _ALLOWED_METHODS:
        return _err(
            "load_tester.invalid_method",
            f"method must be one of {sorted(_ALLOWED_METHODS)}; got '{method}'.",
        )
    return method


def _validate_numeric_limits(
    payload: dict[str, Any],
) -> dict | tuple[int, int, int, int]:
    """Pure: ensure rps/duration/concurrency/expected_status fall in supported ranges."""
    try:
        rps = int(payload.get("rps") or _DEFAULT_RPS)
        duration_s = int(payload.get("duration_seconds") or _DEFAULT_DURATION_S)
        concurrency = int(payload.get("concurrency") or _DEFAULT_CONCURRENCY)
        expected_status = int(payload.get("expected_status") or _DEFAULT_EXPECTED_STATUS)
    except (TypeError, ValueError) as exc:
        return _err(
            "load_tester.invalid_parameter",
            "rps, duration_seconds, concurrency, and expected_status must be integers: "
            f"{exc}",
        )
    issues: list[str] = []
    if rps < 1 or rps > _MAX_RPS:
        issues.append(f"rps must be between 1 and {_MAX_RPS}; got {rps}")
    if duration_s < 1 or duration_s > _MAX_DURATION_S:
        issues.append(
            f"duration_seconds must be between 1 and {_MAX_DURATION_S}; got {duration_s}"
        )
    if concurrency < 1 or concurrency > _MAX_CONCURRENCY:
        issues.append(
            f"concurrency must be between 1 and {_MAX_CONCURRENCY}; got {concurrency}"
        )
    if issues:
        return _err("load_tester.limits_exceeded", "; ".join(issues))
    return rps, duration_s, concurrency, expected_status


def _validate_headers_body(
    payload: dict[str, Any],
) -> dict | tuple[dict[str, str], str | None]:
    """Pure: shape headers/body into normalised types or return an error envelope."""
    raw_headers = payload.get("headers")
    headers: dict[str, str] = {}
    if raw_headers is not None:
        if not isinstance(raw_headers, dict):
            return _err("load_tester.invalid_headers", "headers must be an object.")
        headers = {str(k): str(v) for k, v in raw_headers.items()}
    raw_body = payload.get("body")
    body = str(raw_body) if raw_body is not None else None
    return headers, body


def _unique_errors(results: list[_Result]) -> list[str]:
    """Pure: first ``_MAX_UNIQUE_ERRORS`` distinct error strings, in encounter order."""
    seen: set[str] = set()
    out: list[str] = []
    for _, _, err_msg in results:
        if err_msg and err_msg not in seen:
            seen.add(err_msg)
            out.append(err_msg)
            if len(out) >= _MAX_UNIQUE_ERRORS:
                break
    return out


def _latency_stats(latencies_ms: list[float]) -> dict[str, float]:
    """Pure: percentile + mean/min/max/stddev summary stats."""
    sorted_lat = sorted(latencies_ms)
    return {
        "p50": _percentile(sorted_lat, 50),
        "p75": _percentile(sorted_lat, 75),
        "p95": _percentile(sorted_lat, 95),
        "p99": _percentile(sorted_lat, 99),
        "mean": round(statistics.fmean(latencies_ms), 2) if latencies_ms else 0.0,
        "min": round(min(latencies_ms), 2) if latencies_ms else 0.0,
        "max": round(max(latencies_ms), 2) if latencies_ms else 0.0,
        "std_dev": round(statistics.stdev(latencies_ms), 2) if len(latencies_ms) > 1 else 0.0,
    }


def _aggregate_results(
    results: list[_Result], duration_actual_ms: int, expected_status: int,
) -> dict[str, Any]:
    """Pure: derive counters, status histogram, latency stats, and summary line."""
    total = len(results)
    latencies_ms = [r[0] for r in results]
    status_counts: Counter[str] = Counter(str(r[1]) for r in results if r[1] != 0)
    transport_errors = sum(1 for r in results if r[1] == 0)
    if transport_errors:
        status_counts["0"] = transport_errors
    success_count = sum(1 for r in results if r[1] == expected_status)
    error_count = total - success_count
    error_rate = round(error_count / total, 4) if total else 0.0
    throughput_rps = (
        round(total / (duration_actual_ms / 1000), 2) if duration_actual_ms else 0.0
    )
    latency_ms = _latency_stats(latencies_ms)
    summary = (
        f"p50={latency_ms['p50']}ms p95={latency_ms['p95']}ms p99={latency_ms['p99']}ms "
        f"throughput={throughput_rps}rps "
        f"errors={round(error_rate * 100, 1)}% "
        f"on {total} requests"
    )
    return {
        "total_requests": total,
        "success_count": success_count,
        "error_count": error_count,
        "error_rate": error_rate,
        "duration_actual_ms": duration_actual_ms,
        "throughput_rps": throughput_rps,
        "latency_ms": latency_ms,
        "status_codes": dict(status_counts),
        "errors": _unique_errors(results),
        "histogram": _build_histogram(latencies_ms),
        "summary": summary,
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a real HTTP load test and return latency percentiles + error rates.

    Why: a sandboxed thread-pool tester gives a real apples-to-apples
    measurement against external services without the agent caller having
    to provision wrk/hey/k6 themselves.
    """
    # NEW-6 (sweep 2026-05-20): structured envelope, not bare TypeError —
    # see cve_lookup.run for the rationale (avoid HTTP 500 stack traces
    # on a payload-type error that should be a clean 422).
    if not isinstance(payload, dict):
        return _err(
            "load_tester.invalid_payload",
            f"payload must be dict, got {type(payload).__name__}",
        )
    url = _validate_url(payload)
    if isinstance(url, dict):
        return url
    method = _validate_method(payload)
    if isinstance(method, dict):
        return method
    limits = _validate_numeric_limits(payload)
    if isinstance(limits, dict):
        return limits
    rps, duration_s, concurrency, expected_status = limits
    hb = _validate_headers_body(payload)
    if isinstance(hb, dict):
        return hb
    headers, body = hb
    test_start = time.monotonic()
    raw_results = _run_load_test(
        url=url, method=method, headers=headers, body=body,
        expected_status=expected_status, duration_s=duration_s, concurrency=concurrency,
    )
    duration_actual_ms = int((time.monotonic() - test_start) * 1000)
    if not raw_results:
        return _err(
            "load_tester.unreachable",
            "No responses were recorded. The target may be unreachable.",
        )
    return {"url": url, "method": method, **_aggregate_results(
        raw_results, duration_actual_ms, expected_status,
    )}
