"""Live HTTP endpoint load-tester: fires N concurrent requests and returns latency stats.

``run()`` sends ``requests`` HTTP calls to the target URL with the given
``concurrency`` using a ``ThreadPoolExecutor``, then aggregates the results into
percentile latencies, a latency histogram, per-status-code counts, and a sample
of any error messages.

All outbound URLs are validated by ``core.url_security.validate_outbound_url``
before any network I/O — private IPs, loopback, and URL-encoded variants are
blocked.

Payload schema
--------------
Required:
  ``url`` (str)                 — target URL (public HTTPS only in prod)

Optional:
  ``method`` (str, default GET) — HTTP method; one of GET POST PUT PATCH DELETE HEAD OPTIONS
  ``headers`` (dict)            — extra request headers
  ``body`` (dict|list|str)      — request body; dicts/lists sent as JSON, strings/bytes raw
  ``requests`` (int, default 50, max 200)    — total request count
  ``concurrency`` (int, default 5)           — parallel workers (capped at ``requests``)
  ``timeout_seconds`` (float, default 5.0, max 10.0) — per-request timeout

Response shape
--------------
On success: ``{url, method, requests, concurrency, success_count, failure_count,
status_counts, p50_latency_ms, p95_latency_ms, p99_latency_ms, avg_latency_ms,
histogram, sample_errors, execution_time_ms, billing_units_actual}``

On error: ``{error: {code, message}}``
"""
from __future__ import annotations

import math
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from core.url_security import validate_outbound_url


_MAX_REQUESTS = 200
_MAX_TIMEOUT_SECONDS = 10.0
_HISTOGRAM_BUCKETS_MS = [50, 100, 250, 500, 1000, 2000, 5000]


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _percentile(samples: list[float], pct: float) -> int:
    if not samples:
        return 0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return int(round(ordered[index]))


def _histogram(samples: list[float]) -> list[dict[str, Any]]:
    buckets: list[dict[str, Any]] = []
    previous = 0
    for upper in _HISTOGRAM_BUCKETS_MS:
        count = sum(1 for sample in samples if previous <= sample < upper)
        buckets.append({"lt_ms": upper, "count": count})
        previous = upper
    buckets.append({"gte_ms": _HISTOGRAM_BUCKETS_MS[-1], "count": sum(1 for sample in samples if sample >= _HISTOGRAM_BUCKETS_MS[-1])})
    return buckets


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Fire repeated HTTP requests to a URL and return latency statistics.

    Required: ``url``. Optional: ``method`` (default GET), ``headers``, ``body``,
    ``requests`` (default 50), ``concurrency`` (default 5), ``timeout_seconds``.
    Returns ``{url, method, requests, concurrency, success_count, failure_count,
    status_counts, p50/p95/p99_latency_ms, histogram, sample_errors}``.
    """
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("live_endpoint_tester.missing_url", "url is required.")
    try:
        url = validate_outbound_url(raw_url, "url")
    except ValueError as exc:
        return _err("live_endpoint_tester.invalid_url", str(exc))

    method = str(payload.get("method") or "GET").strip().upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        return _err("live_endpoint_tester.invalid_method", f"Unsupported method '{method}'.")

    headers = payload.get("headers") or {}
    if not isinstance(headers, dict):
        return _err("live_endpoint_tester.invalid_headers", "headers must be an object.")

    body = payload.get("body")
    requests_count = max(1, min(int(payload.get("requests") or 50), _MAX_REQUESTS))
    concurrency = max(1, min(int(payload.get("concurrency") or 5), requests_count))
    timeout_seconds = max(0.1, min(float(payload.get("timeout_seconds") or 5.0), _MAX_TIMEOUT_SECONDS))

    session = requests.Session()
    session.headers.update({"User-Agent": "aztea-live-endpoint-tester/1.0"})

    def _one_call() -> dict[str, Any]:
        started = time.monotonic()
        try:
            response = session.request(
                method,
                url,
                headers=headers,
                json=body if isinstance(body, (dict, list)) else None,
                data=body if isinstance(body, (str, bytes)) else None,
                timeout=timeout_seconds,
                allow_redirects=False,
            )
            elapsed_ms = (time.monotonic() - started) * 1000
            return {
                "ok": response.ok,
                "status_code": response.status_code,
                "latency_ms": elapsed_ms,
                "bytes_out": len(response.content or b""),
            }
        except requests.exceptions.Timeout:
            return {"ok": False, "status_code": 0, "latency_ms": timeout_seconds * 1000, "error": "timeout"}
        except requests.RequestException as exc:
            return {"ok": False, "status_code": 0, "latency_ms": (time.monotonic() - started) * 1000, "error": type(exc).__name__}

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_one_call) for _ in range(requests_count)]
        for future in as_completed(futures):
            results.append(future.result())
    session.close()

    latencies = [float(item["latency_ms"]) for item in results]
    status_counts = Counter(str(item.get("status_code", 0)) for item in results)
    success_count = sum(1 for item in results if item.get("ok"))
    failure_count = len(results) - success_count
    sample_errors = [item.get("error") for item in results if item.get("error")][:5]

    return {
        "url": url,
        "method": method,
        "requests": requests_count,
        "concurrency": concurrency,
        "success_count": success_count,
        "failure_count": failure_count,
        "status_counts": dict(status_counts),
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "p99_latency_ms": _percentile(latencies, 99),
        "avg_latency_ms": int(round(statistics.fmean(latencies))) if latencies else 0,
        "histogram": _histogram(latencies),
        "sample_errors": sample_errors,
        "execution_time_ms": int((time.monotonic() - started) * 1000),
        "billing_units_actual": requests_count,
    }
