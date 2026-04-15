"""
error_codes.py — canonical machine-readable API error taxonomy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
AGENT_TIMEOUT = "AGENT_TIMEOUT"
SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
UNAUTHORIZED = "UNAUTHORIZED"
RATE_LIMITED = "RATE_LIMITED"
AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
INVALID_INPUT = "INVALID_INPUT"
DISPUTE_WINDOW_CLOSED = "DISPUTE_WINDOW_CLOSED"
AGENT_SUSPENDED = "AGENT_SUSPENDED"
DISPUTE_CLAWBACK_INSUFFICIENT_BALANCE = "DISPUTE_CLAWBACK_INSUFFICIENT_BALANCE"
DISPUTE_SETTLEMENT_INSUFFICIENT_BALANCE = "DISPUTE_SETTLEMENT_INSUFFICIENT_BALANCE"

DEFAULT_BY_STATUS: dict[int, str] = {
    400: INVALID_INPUT,
    401: UNAUTHORIZED,
    402: INSUFFICIENT_FUNDS,
    403: UNAUTHORIZED,
    404: INVALID_INPUT,
    409: INVALID_INPUT,
    410: AGENT_TIMEOUT,
    413: INVALID_INPUT,
    422: INVALID_INPUT,
    429: RATE_LIMITED,
    500: "INTERNAL_ERROR",
    502: "UPSTREAM_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def make_error(
    error: str,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": str(error).strip() or INVALID_INPUT,
        "message": str(message).strip() or "Request failed.",
        "data": dict(data or {}),
    }
    return payload
