"""
error_codes.py — canonical machine-readable API error taxonomy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

INSUFFICIENT_FUNDS = "payment.insufficient_funds"
AGENT_TIMEOUT = "job.lease_expired"
SCHEMA_MISMATCH = "schema.mismatch"
JOB_NOT_FOUND = "job.not_found"
UNAUTHORIZED = "auth.forbidden"
RATE_LIMITED = "rate.limit_exceeded"
AGENT_NOT_FOUND = "agent.not_found"
INVALID_INPUT = "request.invalid_input"
DISPUTE_WINDOW_CLOSED = "dispute.window_closed"
AGENT_SUSPENDED = "agent.suspended"
DISPUTE_CLAWBACK_INSUFFICIENT_BALANCE = "dispute.clawback_insufficient_balance"
DISPUTE_SETTLEMENT_INSUFFICIENT_BALANCE = "dispute.settlement_insufficient_balance"

DEFAULT_BY_STATUS: dict[int, str] = {
    400: INVALID_INPUT,
    401: "auth.invalid_key",
    402: INSUFFICIENT_FUNDS,
    403: "auth.forbidden",
    404: INVALID_INPUT,
    409: INVALID_INPUT,
    410: "job.lease_expired",
    413: INVALID_INPUT,
    422: INVALID_INPUT,
    429: RATE_LIMITED,
    500: "server.internal_error",
    502: "upstream.unavailable",
    503: "server.unavailable",
}


def make_error(
    error: str,
    message: str,
    details: Mapping[str, Any] | Any | None = None,
    *,
    data: Mapping[str, Any] | Any | None = None,
) -> dict[str, Any]:
    normalized_details = details if details is not None else data
    payload: dict[str, Any] = {
        "error": str(error).strip() or "request.invalid_input",
        "message": str(message).strip() or "Request failed.",
        "details": normalized_details,
    }
    return payload
