"""Call-history helpers for estimation and recent performance telemetry."""

from __future__ import annotations

import json
import math
from typing import Any

from .core_schema import _conn, _to_non_negative_float, _to_non_negative_int

_CALL_RING_LIMIT = 200


def normalize_call_ring(raw_ring: Any) -> list[dict[str, int]]:
    if isinstance(raw_ring, str):
        try:
            raw_ring = json.loads(raw_ring)
        except json.JSONDecodeError:
            raw_ring = []
    if not isinstance(raw_ring, list):
        return []

    normalized: list[dict[str, int]] = []
    for item in raw_ring:
        if not isinstance(item, dict):
            continue
        latency_ms = _to_non_negative_int(item.get("latency_ms"), default=-1)
        price_cents = _to_non_negative_int(item.get("price_cents"), default=-1)
        if latency_ms < 0 or price_cents < 0:
            continue
        normalized.append({"latency_ms": latency_ms, "price_cents": price_cents})
    return normalized[-_CALL_RING_LIMIT:]


def append_call_ring_sample(
    raw_ring: Any,
    *,
    latency_ms: float,
    price_cents: int,
    limit: int = _CALL_RING_LIMIT,
) -> str:
    ring = normalize_call_ring(raw_ring)
    ring.append(
        {
            "latency_ms": _to_non_negative_int(round(float(latency_ms)), default=0),
            "price_cents": _to_non_negative_int(price_cents, default=0),
        }
    )
    if len(ring) > limit:
        ring = ring[-limit:]
    return json.dumps(ring, separators=(",", ":"))


def get_agent_call_ring(agent_id: str) -> list[dict[str, int]]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT call_latency_ring FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"Agent '{agent_id}' not found.")
    return normalize_call_ring(row["call_latency_ring"])


def _percentile(sorted_values: list[int], quantile: float) -> int:
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return int(sorted_values[0])
    position = max(0.0, min(1.0, quantile)) * (len(sorted_values) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return int(sorted_values[low])
    fraction = position - low
    interpolated = sorted_values[low] + ((sorted_values[high] - sorted_values[low]) * fraction)
    return int(round(interpolated))


def compute_latency_estimate(
    ring: list[dict[str, int]],
    *,
    fallback_latency_ms: float = 0.0,
) -> dict[str, int | str]:
    fallback = _to_non_negative_int(round(_to_non_negative_float(fallback_latency_ms, default=0.0)), default=0)
    latencies = sorted(
        _to_non_negative_int(item.get("latency_ms"), default=-1)
        for item in ring
        if isinstance(item, dict)
    )
    latencies = [value for value in latencies if value >= 0]
    sample_count = len(latencies)
    if sample_count == 0:
        return {
            "p50_latency_ms": fallback,
            "p95_latency_ms": fallback,
            "confidence": "low",
        }
    confidence = "high" if sample_count >= 20 else "medium" if sample_count >= 5 else "low"
    return {
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "confidence": confidence,
    }
