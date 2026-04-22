"""
HTTP exception handlers and error payload normalization for the FastAPI app.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from core import error_codes
from core import logging_utils


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

    if lowered_message.startswith("authorization header missing"):
        return "auth.missing_authorization"
    if lowered_message == "invalid api key.":
        return "auth.invalid_key"
    if lowered_message == "invalid email or password.":
        return "auth.invalid_credentials"
    if lowered_message.startswith("agent-scoped keys cannot"):
        return "auth.insufficient_scope"
    if lowered_message.startswith("this endpoint requires"):
        return "auth.insufficient_scope"
    if lowered_message.startswith("not available for master key"):
        return "auth.insufficient_scope"
    if lowered_message == "not authorized." or lowered_message.startswith("not authorized"):
        return "auth.forbidden"
    if lowered_message.startswith("tool '"):
        return "mcp.tool_not_found"
    if lowered_message.startswith("agent '"):
        return "agent.not_found"
    if lowered_message.startswith("job '"):
        return "job.not_found"
    if lowered_message.startswith("dispute '"):
        return "dispute.not_found"
    if lowered_message.startswith("wallet '"):
        return "wallet.not_found"
    if lowered_message.startswith("invalid status:"):
        return "request.invalid_status"
    if "idempotency-key is too long" in lowered_message:
        return "request.idempotency_key_too_long"
    if lowered_message.startswith("a request with this idempotency-key is still in progress"):
        return "request.idempotency_conflict"
    if lowered_message.startswith("failed to fetch manifest_url"):
        return "onboarding.manifest_fetch_failed"
    if lowered_message.startswith("manifest too large"):
        return "request.payload_too_large"
    if lowered_message.startswith("fetched manifest is empty"):
        return "onboarding.manifest_empty"
    if lowered_message.startswith("failed to create job"):
        return "job.create_failed"
    if lowered_message.startswith("job is not claimable"):
        return "job.not_claimable"
    if lowered_message.startswith("job is not currently claimed by this worker"):
        return "job.claim_missing"
    if lowered_message.startswith("invalid or missing claim_token") or lowered_message.startswith("invalid or stale claim_token"):
        return "job.invalid_claim_token"
    if lowered_message.startswith("unable to heartbeat this job claim"):
        return "job.heartbeat_failed"
    if lowered_message.startswith("unable to release this job claim"):
        return "job.release_failed"
    if lowered_message.startswith("unable to update job status"):
        return "job.transition_failed"
    if lowered_message.startswith("unable to schedule retry for this job"):
        return "job.retry_failed"
    if lowered_message.startswith("upstream agent unreachable"):
        return "agent.upstream_unreachable"
    if lowered_message.startswith("agent endpoint is misconfigured"):
        return "agent.endpoint_misconfigured"
    if lowered_message.startswith("agent execution failed"):
        return "agent.execution_failed"
    if lowered_message.startswith("all llm models rate-limited"):
        return "agent.upstream_rate_limited"
    if lowered_message.startswith("hook not found"):
        return "hook.not_found"
    if lowered_message.startswith("key not found or already revoked"):
        return "auth.key_not_found"
    if lowered_message.startswith("disputes can only be filed for completed jobs"):
        return "dispute.invalid_state"
    if lowered_message.startswith("disputes must be filed before the caller submits a rating"):
        return "dispute.rating_locked"
    if lowered_message.startswith("a dispute already exists for this job"):
        return "dispute.already_exists"
    if lowered_message.startswith("dispute window has expired for this job"):
        return "dispute.window_closed"
    if lowered_message.startswith("job completion timestamp is invalid"):
        return "job.invalid_completion_timestamp"
    if lowered_message.startswith("failed to resolve dispute"):
        return "dispute.resolve_failed"
    if lowered_message.startswith("tool_result payload.correlation_id is required"):
        return "job.invalid_tool_result"
    if lowered_message.startswith("unknown tool_result correlation_id"):
        return "job.invalid_tool_result"
    if lowered_message.startswith("unsupported job message type"):
        return "job.invalid_message_type"
    if lowered_message.startswith("agent.md spec not found"):
        return "onboarding.spec_not_found"
    if lowered_message.startswith("cursor must not be empty") or lowered_message.startswith("invalid cursor"):
        return "request.invalid_cursor"
    if lowered_message.startswith("limit must be > 0"):
        return "request.invalid_limit"
    if lowered_message.startswith("sla_seconds must be > 0"):
        return "request.invalid_sla_seconds"
    if lowered_message.startswith("max_mismatches must be > 0"):
        return "request.invalid_max_mismatches"
    if lowered_message.startswith("rank_by must be one of"):
        return "request.invalid_rank_by"
    if lowered_message.startswith("authentication service is temporarily unavailable"):
        return "auth.service_unavailable"
    if lowered_message.startswith("master key cannot"):
        return "auth.master_forbidden"
    if lowered_message.startswith("only the original caller can rate this job"):
        return "job.rating_forbidden"
    if lowered_message.startswith("only the job's agent owner can rate the caller"):
        return "job.rating_forbidden"
    if lowered_message.startswith("ratings are locked once a dispute is filed"):
        return "dispute.rating_locked"
    if lowered_message.startswith("this endpoint requires caller or worker scope"):
        return "auth.insufficient_scope"
    return _default_error_code_for_request(status_code, path, lowered_message)


def normalize_error_payload(status_code: int, detail: Any, path: str) -> dict[str, Any]:
    if isinstance(detail, dict):
        raw_error = str(detail.get("error") or "").strip()
        if {"error", "message"}.issubset(detail.keys()):
            details = detail.get("details")
            if details is None and "data" in detail:
                details = detail.get("data")
            return error_codes.make_error(
                raw_error or _error_code_from_message(status_code, path, str(detail.get("message") or "")),
                str(detail.get("message") or "Request failed."),
                details,
            )
        message = str(detail.get("message") or detail.get("detail") or "Request failed.").strip()
        details = {
            str(k): v
            for k, v in detail.items()
            if str(k) not in {"error", "message", "detail", "details", "data"}
        }
        if "details" in detail and detail["details"] is not None:
            details = detail["details"]
        elif "data" in detail and detail["data"] is not None:
            details = detail["data"]
        return error_codes.make_error(
            raw_error or _error_code_from_message(status_code, path, message),
            message,
            details,
        )
    message = str(detail or "Request failed.")
    return error_codes.make_error(
        _error_code_from_message(status_code, path, message),
        message,
        None,
    )


def with_request_id(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    rid = getattr(request.state, "request_id", None)
    if rid and "request_id" not in payload:
        return {**payload, "request_id": str(rid)}
    return payload


def register_exception_handlers(app: FastAPI, *, logger: logging.Logger) -> None:
    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        payload = normalize_error_payload(exc.status_code, exc.detail, request.url.path)
        return JSONResponse(content=with_request_id(request, payload), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
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
    async def _rate_limit_exception_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
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
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logging_utils.log_event(
            logger,
            logging.ERROR,
            "server.unhandled_exception",
            {"method": request.method, "path": request.url.path},
        )
        logger.exception("unhandled_exception")
        payload = error_codes.make_error("server.internal_error", "Internal server error.")
        return JSONResponse(content=with_request_id(request, payload), status_code=500)
