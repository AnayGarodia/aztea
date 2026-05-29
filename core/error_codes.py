"""
error_codes.py — canonical machine-readable API error taxonomy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

INSUFFICIENT_FUNDS = "payment.insufficient_funds"
WALLET_INSUFFICIENT_AVAILABLE = "wallet.insufficient_available"
AGENT_TIMEOUT = "job.lease_expired"
SCHEMA_MISMATCH = "schema.mismatch"
JOB_NOT_FOUND = "job.not_found"
UNAUTHORIZED = "auth.forbidden"
RATE_LIMITED = "rate.limit_exceeded"
AGENT_NOT_FOUND = "agent.not_found"
INVALID_INPUT = "request.invalid_input"
DISPUTE_WINDOW_CLOSED = "dispute.window_closed"
AGENT_SUSPENDED = "agent.suspended"
AGENT_SUNSET = "agent.sunset"  # 410 Gone — agent removed from public catalog
AGENT_UPSTREAM_TIMEOUT = "agent.upstream_timeout"  # 504 — pool exhausted/proc slow
AGENT_INVALID_INPUT = "agent.invalid_input"  # 422 — agent rejected the payload shape
JOB_ALREADY_RATED = "job.already_rated"  # 409 — caller has already rated this job
DISPUTE_CLAWBACK_INSUFFICIENT_BALANCE = "dispute.clawback_insufficient_balance"
DISPUTE_SETTLEMENT_INSUFFICIENT_BALANCE = "dispute.settlement_insufficient_balance"
DISPUTE_FILING_DEPOSIT_INSUFFICIENT_BALANCE = (
    "dispute.filing_deposit_insufficient_balance"
)
BUDGET_EXCEEDED = "job.budget_exceeded"
SPEND_LIMIT_EXCEEDED = "payment.spend_limit_exceeded"
# 2026-05-19 (B1): distinct envelope for the per-job hard cap so callers can
# distinguish "I asked for a cap" (this one) from "the API key has a cap"
# (still SPEND_LIMIT_EXCEEDED). Both 402; the difference is which knob to
# tune.
JOB_PER_JOB_CAP_EXCEEDED = "job.per_job_cap_exceeded"
# 2026-05-19 (B3): server-side session budget exceeded.
WALLET_SESSION_BUDGET_EXCEEDED = "wallet.session_budget_exceeded"
VERIFIED_CONTRACT_REQUIRED = "job.verified_contract_required"
ORCHESTRATION_DEPTH_EXCEEDED = "job.orchestration_depth_exceeded"
DEPOSIT_BELOW_MINIMUM = "payment.deposit_below_minimum"
VALIDATION_ERROR = "request.validation_error"
LEGAL_VERSION_MISMATCH = "auth.legal_version_mismatch"
INSUFFICIENT_SCOPE = "auth.insufficient_scope"
CHARGE_EXCEEDS_LISTED_PRICE = "job.charge_exceeds_listed_price"
INVALID_CHARGE_AMOUNT = "job.invalid_charge_amount"
INVALID_OR_EXPIRED_TOKEN = "auth.invalid_or_expired_token"
REGISTRY_ENDPOINT_UNREACHABLE = "registry.endpoint_unreachable"
REGISTRY_MANIFEST_UNREACHABLE = "registry.manifest_unreachable"

# Agent proxy errors — all result in automatic refund
AGENT_CALL_TIMEOUT = "agent.call_timeout"
AGENT_ENDPOINT_OFFLINE = "agent.endpoint_offline"
AGENT_INTERNAL_ERROR = "agent.internal_error"
AGENT_REJECTED_REQUEST = "agent.rejected_request"
AGENT_INVALID_RESPONSE = "agent.invalid_response"
AGENT_RESPONSE_TOO_LARGE = "agent.response_too_large"
# 503-class: the outbound HTTP pool to this host is full. Caller should retry
# with Retry-After hint. Distinct from AGENT_ENDPOINT_OFFLINE so SDKs can
# auto-retry pool-saturation but surface offline-host as a user-visible error.
OUTBOUND_POOL_SATURATED = "outbound.pool_saturated"
# 1.7.1 — distinct envelope for "agent ran fine but the URL/domain you gave
# can't be reached." Lets SDKs surface 'fix the input' vs 'retry later' vs
# 'page oncall'. 422-class.
AGENT_TARGET_UNREACHABLE = "agent.target_unreachable"

# Input / payload errors
PAYLOAD_TOO_LARGE = "request.payload_too_large"
INPUT_SCHEMA_VIOLATION = "request.input_schema_violation"

# Registry / agent management
REGISTRY_AGENT_LIMIT = "registry.agent_limit_reached"
REGISTRY_INVALID_SCHEMA = "registry.invalid_schema"

# Job lifecycle structured errors
JOB_NOT_CLAIMABLE = "job.not_claimable"
JOB_HEARTBEAT_FAILED = "job.heartbeat_failed"
JOB_COMPLETE_FAILED = "job.complete_failed"
JOB_FAIL_FAILED = "job.fail_failed"
JOB_INVALID_STATE = "job.invalid_state"
JOB_BATCH_PARTIAL_FAILURE = "job.batch.partial_failure"
JOB_NOT_FOUND_404 = "job.not_found"  # alias used for explicit 404 returns
JOB_CREATE_FAILED = "job.create_failed"
JOB_EXECUTION_FAILED = "job.execution_failed"
JOB_INVALID_CLAIM_TOKEN = "job.invalid_claim_token"

# Dispute lifecycle structured errors
DISPUTE_FILING_FAILED = "dispute.filing_failed"

# Auth / account limits
AUTH_KEY_LIMIT = "auth.key_limit_reached"
AUTH_HOOK_LIMIT = "auth.hook_limit_reached"

# Self-interaction blocks
JOB_SELF_RATE = "job.self_rate_not_allowed"
JOB_SELF_DISPUTE = "job.self_dispute_not_allowed"
JOB_RATE_STATUS_INVALID = "job.rate_invalid_status"
JOB_INVALID_RATING = "job.invalid_rating"

# Workspace lifecycle errors
WORKSPACE_NOT_FOUND = "workspace.not_found"
WORKSPACE_FORBIDDEN = "workspace.forbidden"
WORKSPACE_SEALED = "workspace.sealed"
WORKSPACE_QUOTA_EXCEEDED = "workspace.quota_exceeded"
WORKSPACE_ARTIFACT_NOT_FOUND = "workspace.artifact.not_found"
WORKSPACE_ARTIFACT_TOO_LARGE = "workspace.artifact.too_large"
WORKSPACE_ARTIFACT_NAME_INVALID = "workspace.artifact.name_invalid"
WORKSPACE_ARTIFACT_CONFLICT = "workspace.artifact.conflict"
WORKSPACE_BACKING_EVICTED = "workspace.backing.evicted"
WORKSPACE_SEAL_SIGNING_FAILED = "workspace.seal.signing_failed"

# Phase 0 (2026-05-28): auto-hire refusal reason taxonomy. These mirror
# the `reason` field on Decision objects returned from
# core/registry/auto_hire.py::decide(). LOCKED — additive-only stability
# promised to callers writing switch statements against them. Add a new
# code by appending here; never repurpose an existing string.
AUTO_HIRE_NO_MATCH = "auto_hire.no_match"
AUTO_HIRE_LOW_CONFIDENCE = "auto_hire.low_confidence"
AUTO_HIRE_LOW_TRUST = "auto_hire.low_trust"
AUTO_HIRE_LOW_SUCCESS_RATE = "auto_hire.low_success_rate"
AUTO_HIRE_BROKEN_AGENT = "auto_hire.broken_agent"
AUTO_HIRE_BETA_AGENT = "auto_hire.beta_agent"
AUTO_HIRE_PRICE_EXCEEDS_MAX = "auto_hire.price_exceeds_max"
AUTO_HIRE_MISSING_FIELDS = "auto_hire.missing_fields"
AUTO_HIRE_DISABLED = "auto_hire.disabled"
AUTO_HIRE_EMPTY_INTENT = "auto_hire.empty_intent"
# Phase 5
AUTO_HIRE_COMPOUND_INTENT = "auto_hire.compound_intent"
# Phase 1 B4 reserved for future tiebreaker-specific outcomes
AUTO_HIRE_TIEBREAKER_FAILED = "auto_hire.tiebreaker_failed"
# Phase 0.5 C2 reserved for the "agent just got auto-flipped" code
AUTO_HIRE_AGENT_RECENTLY_FLIPPED_BROKEN = "auto_hire.agent_recently_flipped_broken"

AUTO_HIRE_REASONS: frozenset[str] = frozenset({
    AUTO_HIRE_NO_MATCH,
    AUTO_HIRE_LOW_CONFIDENCE,
    AUTO_HIRE_LOW_TRUST,
    AUTO_HIRE_LOW_SUCCESS_RATE,
    AUTO_HIRE_BROKEN_AGENT,
    AUTO_HIRE_BETA_AGENT,
    AUTO_HIRE_PRICE_EXCEEDS_MAX,
    AUTO_HIRE_MISSING_FIELDS,
    AUTO_HIRE_DISABLED,
    AUTO_HIRE_EMPTY_INTENT,
    AUTO_HIRE_COMPOUND_INTENT,
    AUTO_HIRE_TIEBREAKER_FAILED,
    AUTO_HIRE_AGENT_RECENTLY_FLIPPED_BROKEN,
})

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
    """Build a structured error response dict ``{error, message, details}``.

    ``error`` should be a dot-namespaced code from the taxonomy in
    ``core/error_codes.py`` (e.g. ``"request.invalid_input"``). ``details``
    (or the alias ``data``) carries any additional structured context.
    """
    normalized_details = details if details is not None else data
    payload: dict[str, Any] = {
        "error": str(error).strip() or "request.invalid_input",
        "message": str(message).strip() or "Request failed.",
        "details": normalized_details,
    }
    return payload
