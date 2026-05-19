"""HTTP exception handlers and error payload normalisation for the FastAPI app.

This module guarantees that every error response the platform emits uses the
stable envelope defined in ``core.error_codes.make_error``:

::

    {
        "error":      "<dot.namespaced.code>",
        "message":    "<human-readable, actionable text>",
        "details":    <null | structured context>,
        "request_id": "<X-Request-ID header value>",
    }

Responsibilities:

- ``HTTPException`` raised anywhere in a route handler is normalised so the
  ``detail`` field (dict or string) collapses into the ``error`` / ``message``
  / ``details`` fields above. Legacy string details are mapped to specific
  error codes via ``_error_code_from_message`` — this keeps the client SDKs
  able to branch on machine-readable codes without forcing every route to
  spell them out explicitly.
- ``RequestValidationError`` from FastAPI (body/query/path validation) is
  converted into ``request.invalid_input`` with the sanitised pydantic
  error list under ``details.errors``.
- ``RateLimitExceeded`` from slowapi returns a ``rate_limit_exceeded`` payload
  plus a ``Retry-After`` header in seconds.
- Any other unhandled exception logs a ``server.unhandled_exception`` event
  and returns a generic ``server.internal_error`` 500 so we never leak stack
  traces to the wire.

The SPA fallback (``server.application_parts.part_012.spa_fallback``) and the
``api_prefix_compat`` middleware (``part_001``) sit **outside** this module but
rely on the same envelope, so users always see structured, actionable errors
regardless of how the request reached FastAPI.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from core import error_codes, logging_utils

# Module-level logger so normalize_error_payload (called outside the
# register_exception_handlers context) can still record sanitisation
# events for ops. Tests can capture via caplog.
_LEAK_SANITIZER_LOG = logging.getLogger("aztea.error_handlers")


# Helpers for the ERROR_CODE_PATTERNS table below. Each pattern is a small
# predicate over the lowercased message. Keeping these as named factories
# (instead of inlining lambdas) lets the patterns table read top-to-bottom
# the same way the old if-chain did, while eliminating the typo risk of a
# 50-branch ladder.
def _eq(literal: str) -> Callable[[str], bool]:
    return lambda m: m == literal


def _starts(prefix: str) -> Callable[[str], bool]:
    return lambda m: m.startswith(prefix)


def _starts_any(*prefixes: str) -> Callable[[str], bool]:
    return lambda m: any(m.startswith(p) for p in prefixes)


def _contains(needle: str) -> Callable[[str], bool]:
    return lambda m: needle in m


# Order is preserved from the original if/elif chain so behavior is
# identical: the first matching predicate wins. New patterns must be
# inserted at the position where their specificity demands.
ERROR_CODE_PATTERNS: list[tuple[Callable[[str], bool], str]] = [
    (_starts("authorization header missing"), "auth.missing_authorization"),
    (_eq("invalid api key."), "auth.invalid_key"),
    (_eq("invalid email or password."), "auth.invalid_credentials"),
    (_starts("agent-scoped keys cannot"), "auth.insufficient_scope"),
    (_starts("this endpoint requires"), "auth.insufficient_scope"),
    (_starts("not available for master key"), "auth.insufficient_scope"),
    (_starts("not authorized"), "auth.forbidden"),
    (_starts("tool '"), "mcp.tool_not_found"),
    (_starts("agent '"), "agent.not_found"),
    (_starts("job '"), "job.not_found"),
    (_starts("dispute '"), "dispute.not_found"),
    (_starts("wallet '"), "wallet.not_found"),
    (_starts("invalid status:"), "request.invalid_status"),
    (_contains("idempotency-key is too long"), "request.idempotency_key_too_long"),
    (
        _starts("a request with this idempotency-key is still in progress"),
        "request.idempotency_conflict",
    ),
    (_starts("failed to fetch manifest_url"), "onboarding.manifest_fetch_failed"),
    (_starts("manifest too large"), "request.payload_too_large"),
    (_starts("fetched manifest is empty"), "onboarding.manifest_empty"),
    (_starts("failed to create job"), "job.create_failed"),
    (_starts("job is not claimable"), "job.not_claimable"),
    (_starts("job is not currently claimed by this worker"), "job.claim_missing"),
    (
        _starts_any("invalid or missing claim_token", "invalid or stale claim_token"),
        "job.invalid_claim_token",
    ),
    (_starts("unable to heartbeat this job claim"), "job.heartbeat_failed"),
    (_starts("unable to release this job claim"), "job.release_failed"),
    (_starts("unable to update job status"), "job.transition_failed"),
    (_starts("unable to schedule retry for this job"), "job.retry_failed"),
    (_starts("upstream agent unreachable"), "agent.upstream_unreachable"),
    (_starts("agent endpoint is misconfigured"), "agent.endpoint_misconfigured"),
    (_starts("agent execution failed"), "agent.execution_failed"),
    (_starts("all llm models rate-limited"), "agent.upstream_rate_limited"),
    (_starts("hook not found"), "hook.not_found"),
    (_starts("key not found or already revoked"), "auth.key_not_found"),
    (
        _starts("disputes can only be filed for completed jobs"),
        "dispute.invalid_state",
    ),
    (
        _starts("disputes must be filed before the caller submits a rating"),
        "dispute.rating_locked",
    ),
    (_starts("a dispute already exists for this job"), "dispute.already_exists"),
    (_starts("dispute window has expired for this job"), "dispute.window_closed"),
    (
        _starts("job completion timestamp is invalid"),
        "job.invalid_completion_timestamp",
    ),
    (_starts("failed to resolve dispute"), "dispute.resolve_failed"),
    (
        _starts_any(
            "tool_result payload.correlation_id is required",
            "unknown tool_result correlation_id",
        ),
        "job.invalid_tool_result",
    ),
    (_starts("unsupported job message type"), "job.invalid_message_type"),
    (_starts("agent.md spec not found"), "onboarding.spec_not_found"),
    (
        _starts_any("cursor must not be empty", "invalid cursor"),
        "request.invalid_cursor",
    ),
    (_starts("limit must be > 0"), "request.invalid_limit"),
    (_starts("sla_seconds must be > 0"), "request.invalid_sla_seconds"),
    (_starts("max_mismatches must be > 0"), "request.invalid_max_mismatches"),
    (_starts("rank_by must be one of"), "request.invalid_rank_by"),
    (
        _starts("authentication service is temporarily unavailable"),
        "auth.service_unavailable",
    ),
    (_starts("master key cannot"), "auth.master_forbidden"),
    (
        _starts("only the original caller can rate this job"),
        "job.rating_forbidden",
    ),
    (
        _starts("only the job's agent owner can rate the caller"),
        "job.rating_forbidden",
    ),
    (
        _starts("ratings are locked once a dispute is filed"),
        "dispute.rating_locked",
    ),
    (
        _starts("this endpoint requires caller or worker scope"),
        "auth.insufficient_scope",
    ),
]


def _default_error_code_for_request(status_code: int, path: str, message: str) -> str:
    lowered_path = str(path or "").lower()
    lowered_message = str(message or "").lower()
    if status_code == 404 and lowered_path.startswith("/jobs/"):
        return error_codes.JOB_NOT_FOUND
    if status_code == 404 and lowered_path.startswith("/registry/agents"):
        return error_codes.AGENT_NOT_FOUND
    if status_code == 410:
        return error_codes.AGENT_TIMEOUT
    if status_code == 400 and "dispute window" in lowered_message:
        return error_codes.DISPUTE_WINDOW_CLOSED
    if status_code == 503 and "suspend" in lowered_message:
        return error_codes.AGENT_SUSPENDED
    return error_codes.DEFAULT_BY_STATUS.get(status_code, error_codes.INVALID_INPUT)


def _error_code_from_message(status_code: int, path: str, message: str) -> str:
    lowered_message = str(message or "").strip().lower()
    for predicate, code in ERROR_CODE_PATTERNS:
        if predicate(lowered_message):
            return code
    return _default_error_code_for_request(status_code, path, lowered_message)


# Phase 1, 2026-05-19: boundary sanitiser for raw-exception leak text. The
# 41 ``detail=str(exc)`` sites across the codebase route raw psycopg2 /
# pydantic / sqlalchemy / starlette error messages into the response. The
# global handler used to pass that text through verbatim — so a caller
# sending a NUL byte got back ``"A string literal cannot contain NUL
# (0x00) characters."`` instead of a structured envelope (F20). Each new
# exception type that's added to the upstream libraries is a fresh
# leak waiting to happen.
#
# The sanitiser catches the leak at the global handler: bare-string
# details that look like raw exception text get their user-visible
# message replaced with a generic ``"Request could not be completed."``
# while the raw text is preserved in a structured log event so ops
# observability is unchanged. Structured ``{error, message, ...}``
# details and short user-facing strings ("max_total_cents must be > 0")
# pass through unchanged.
_EXCEPTION_LEAK_SIGNATURES: tuple[str, ...] = (
    "traceback",
    "psycopg2.errors",
    "sqlalchemy.",
    "pydantic.",
    "valueerror:",
    "typeerror:",
    "keyerror:",
    "attributeerror:",
    "filenotfounderror:",
    "ioerror:",
    "oserror:",
    "runtimeerror:",
    "permissionerror:",
    "cannot contain nul",
    "a string literal cannot contain",
    "disallowed cors origin",
    "starlette.exceptions",
    "internalservererror",
    "<class '",
    "at 0x7",
    "at 0x0",
)

_SANITISED_LEAK_MESSAGE = (
    "Request could not be completed. See server logs for details."
)


def _looks_like_exception_leak(message: str) -> bool:
    """Pure: does ``message`` look like raw exception / library internals?

    Why: a bare-string ``detail=str(exc)`` from any of ~41 route sites can
    surface psycopg2 / pydantic / sqlalchemy / starlette internals. These
    leak path info, library names, and PII embedded in inputs (F20 NUL byte
    repro). The signatures below are conservative — they only flag text
    that's UNMISTAKABLY an exception message, never a user-facing string
    like "max_total_cents must be > 0" or "Job 'abc' not found".
    """
    lowered = str(message or "").lower()
    return any(sig in lowered for sig in _EXCEPTION_LEAK_SIGNATURES)


def _sanitize_leak_if_present(
    message: str, status_code: int, path: str, logger: logging.Logger | None = None
) -> str:
    """Side-effect: log the raw leak; return sanitised user-facing message.

    Returns ``message`` unchanged when no leak signature is detected.
    """
    if not _looks_like_exception_leak(message):
        return message
    if logger is not None:
        logging_utils.log_event(
            logger,
            logging.WARNING,
            "server.error_message_sanitized",
            {
                "method": "n/a",  # caller doesn't always have a request
                "path": str(path or ""),
                "status_code": int(status_code),
                "raw_message_prefix": str(message)[:512],
            },
        )
    return _SANITISED_LEAK_MESSAGE


def normalize_error_payload(status_code: int, detail: Any, path: str) -> dict[str, Any]:
    if isinstance(detail, dict):
        raw_error = str(detail.get("error") or "").strip()
        if {"error", "message"}.issubset(detail.keys()):
            details = detail.get("details")
            if details is None and "data" in detail:
                details = detail.get("data")
            raw_msg = str(detail.get("message") or "Request failed.")
            safe_msg = _sanitize_leak_if_present(
                raw_msg, status_code, path, _LEAK_SANITIZER_LOG
            )
            return error_codes.make_error(
                raw_error
                or _error_code_from_message(status_code, path, raw_msg),
                safe_msg,
                details,
            )
        raw_msg = str(
            detail.get("message") or detail.get("detail") or "Request failed."
        ).strip()
        details = {
            str(k): v
            for k, v in detail.items()
            if str(k) not in {"error", "message", "detail", "details", "data"}
        }
        if "details" in detail and detail["details"] is not None:
            details = detail["details"]
        elif "data" in detail and detail["data"] is not None:
            details = detail["data"]
        safe_msg = _sanitize_leak_if_present(
            raw_msg, status_code, path, _LEAK_SANITIZER_LOG
        )
        return error_codes.make_error(
            raw_error or _error_code_from_message(status_code, path, raw_msg),
            safe_msg,
            details,
        )
    raw_msg = str(detail or "Request failed.")
    safe_msg = _sanitize_leak_if_present(
        raw_msg, status_code, path, _LEAK_SANITIZER_LOG
    )
    return error_codes.make_error(
        _error_code_from_message(status_code, path, raw_msg),
        safe_msg,
        None,
    )


def with_request_id(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    rid = getattr(request.state, "request_id", None)
    if rid and "request_id" not in payload:
        return {**payload, "request_id": str(rid)}
    return payload


def register_exception_handlers(app: FastAPI, *, logger: logging.Logger) -> None:
    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        payload = normalize_error_payload(exc.status_code, exc.detail, request.url.path)
        return JSONResponse(
            content=with_request_id(request, payload), status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        def _sanitize(errors):
            clean = []
            for e in errors:
                entry = {k: v for k, v in e.items() if k != "ctx"}
                ctx = e.get("ctx")
                if ctx:
                    entry["ctx"] = {k: str(v) for k, v in ctx.items()}
                clean.append(entry)
            return clean

        payload = error_codes.make_error(
            error_codes.INVALID_INPUT,
            "Request validation failed.",
            {"errors": _sanitize(exc.errors())},
        )
        return JSONResponse(content=with_request_id(request, payload), status_code=422)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_exception_handler(
        request: Request, exc: RateLimitExceeded
    ) -> JSONResponse:
        retry_after = 60
        limit = getattr(exc, "limit", None)
        if limit is not None:
            limit_item = getattr(limit, "limit", None)
            get_expiry = getattr(limit_item, "get_expiry", None)
            if callable(get_expiry):
                try:
                    retry_after = int(get_expiry())
                except Exception:
                    retry_after = 60
        payload = {
            "error": "rate_limit_exceeded",
            "retry_after_seconds": max(1, retry_after),
        }
        payload = with_request_id(request, payload)
        logging_utils.log_event(
            logger,
            logging.WARNING,
            "http.rate_limited",
            {
                "method": request.method,
                "path": request.url.path,
                "retry_after_seconds": payload["retry_after_seconds"],
            },
        )
        return JSONResponse(
            content=payload,
            status_code=429,
            headers={"Retry-After": str(payload["retry_after_seconds"])},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logging_utils.log_event(
            logger,
            logging.ERROR,
            "server.unhandled_exception",
            {"method": request.method, "path": request.url.path},
        )
        logger.exception("unhandled_exception")
        payload = error_codes.make_error(
            "server.internal_error", "Internal server error."
        )
        return JSONResponse(content=with_request_id(request, payload), status_code=500)
