"""Browser playground — `/api/playground/test` and `/api/playground/publish`.

# OWNS: the two HTTP endpoints that back the Wave-3 Monaco playground
#       at `/build`. ``test`` runs a buyer-supplied Python handler in
#       the same sandbox that backs the curated ``python_executor``
#       agent. ``publish`` re-runs the listing-safety + LLM-judge
#       scans, creates a hosted_skill row, and registers the agent so
#       it can be hired.
#
# NOT OWNS: the sandbox itself (``agents.python_executor``), the
#       listing-safety scanner (``core/listing_safety.py``), the LLM
#       judge (``core/listing_safety_judge.py``), the hosted-skill DB
#       layer (``core/hosted_skills.py``), or the audit log
#       (``core/hosted_execution_log.py``).
#
# INVARIANTS:
#   * ``/api/playground/test`` is anonymous-callable. IP-rate-limited
#     at 5/minute via the existing slowapi ``Limiter``.
#   * ``/api/playground/publish`` requires a ``worker``-scoped key. It
#     re-runs the same listing-safety scan the CLI runs, so direct API
#     callers cannot bypass the verifier.
#   * Every test invocation records one row to ``hosted_execution_log``.
#     The hash-only persistence policy in that module applies — never
#     persist raw input/output.
#   * The test endpoint must never persist a hosted_skill row. Only the
#     publish endpoint creates persisted state. Otherwise an anonymous
#     test call could fingerprint the marketplace.
#
# DECISIONS:
#   - Factory pattern matches ``server/routes/public_integrations.py``
#     and ``server/routes/admin_usage.py`` — keeps part_*.py shards
#     under the line-budget.
#   - Single 5/minute IP-rate-limit. Spec mentions a higher per-key
#     authed limit (60/min); for v1 we accept the conservative limit
#     for everyone since interactive playground use cases easily fit
#     within 5/min. Higher-volume integrators use ``/registry/agents/
#     {id}/call`` with their authed key, which has its own bucket.
#   - The test endpoint accepts an optional ``client_id`` for analytics
#     correlation across multiple Test → Publish loops; the publish
#     endpoint accepts a ``draft_id`` so the UI can stitch the flow
#     together. Neither is required.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from agents import python_executor as _python_executor
from core import error_codes, hosted_execution_log, listing_safety

_LOG = logging.getLogger(__name__)

_TEST_RATE_LIMIT = "5/minute"
_TEST_DEFAULT_TIMEOUT_S = 5
_TEST_HARD_TIMEOUT_S = 10

# Cap the source-size we accept on the test endpoint. The buyer-facing
# Monaco editor caps client-side, but a direct API caller could ship a
# multi-MB payload that defeats the static scanner.
_MAX_SOURCE_CHARS = 32_000
_MAX_INPUT_PAYLOAD_CHARS = 8_000


def _playground_enabled() -> bool:
    """Pure: read the master kill-switch env var. When falsy, both
    endpoints respond 503 with a structured pointer."""
    raw = os.environ.get("AZTEA_PLAYGROUND_ENABLED", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _playground_disabled_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=error_codes.make_error(
            "playground.disabled",
            (
                "The browser playground is currently disabled on this "
                "Aztea instance. Set AZTEA_PLAYGROUND_ENABLED=1 to "
                "enable it after the sandbox-escape suite has been "
                "verified green."
            ),
        ),
    )


def _validate_test_body(body: dict[str, Any]) -> tuple[str, Any, int]:
    """Pure: extract + validate the source / input_payload / timeout.

    Returns ``(source, input_payload, timeout_s)``. Raises HTTPException
    with a structured envelope on any validation failure.
    """
    source = body.get("source")
    if not isinstance(source, str) or not source.strip():
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                "source is required and must be a non-empty string.",
            ),
        )
    if len(source) > _MAX_SOURCE_CHARS:
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                "playground.source_too_large",
                f"Source code exceeds the {_MAX_SOURCE_CHARS}-character limit.",
                details={"supplied_chars": len(source), "max_chars": _MAX_SOURCE_CHARS},
            ),
        )
    input_payload = body.get("input_payload") or {}
    if not isinstance(input_payload, (dict, str, list)):
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                "input_payload must be a JSON object, array, or string.",
            ),
        )
    # Defensive size cap — the python_executor itself enforces this too,
    # but rejecting at the route boundary saves a subprocess spawn.
    serialized_len = len(str(input_payload))
    if serialized_len > _MAX_INPUT_PAYLOAD_CHARS:
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                "playground.input_too_large",
                f"input_payload exceeds the {_MAX_INPUT_PAYLOAD_CHARS}-char limit.",
                details={"supplied_chars": serialized_len},
            ),
        )
    timeout_raw = body.get("timeout_s") or body.get("timeout") or _TEST_DEFAULT_TIMEOUT_S
    try:
        timeout_s = int(timeout_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                "timeout_s must be an integer between 1 and 10.",
            ),
        )
    if not 1 <= timeout_s <= _TEST_HARD_TIMEOUT_S:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                f"timeout_s must be between 1 and {_TEST_HARD_TIMEOUT_S}.",
                details={"supplied": timeout_s, "max": _TEST_HARD_TIMEOUT_S},
            ),
        )
    return source, input_payload, timeout_s


def _run_listing_safety_or_block(source: str) -> None:
    """Side effect: invoke the static scanner. Raises 422 with the
    structured findings if anything blocks.

    Why this runs on /test, not just /publish: a buyer-supplied source
    that imports ``subprocess`` would be blocked at the static layer
    AND at the audit hook, but we'd rather refuse before spending a
    subprocess spawn + an LLM judge call. The test endpoint is the
    cheap-first-line defence — anonymous traffic should be rejected
    early when the static signal is clear.
    """
    findings = listing_safety.scan_python_handler(source)
    if listing_safety.has_block(findings):
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                "playground.listing_safety_blocked",
                "Source failed the listing-safety scan.",
                details={
                    "findings": [
                        {
                            "code": f.code,
                            "level": f.level,
                            "message": f.message,
                            "detail": f.detail,
                        }
                        for f in findings
                        if f.level == listing_safety.LEVEL_BLOCK
                    ],
                },
            ),
        )


def _record_test_invocation(
    *,
    source: str,
    input_payload: Any,
    result: dict[str, Any],
    caller_owner_id: str | None,
    caller_key_id: str | None,
    duration_ms: int,
) -> str | None:
    """Fire-and-forget audit row. Never raises."""
    was_killed = bool(result.get("timed_out"))
    kill_reason = "timeout" if was_killed else None
    sandbox_exit_code = int(result.get("exit_code") or 0)
    return hosted_execution_log.record_execution(
        surface="playground_test",
        execution_time_ms=duration_ms,
        sandbox_exit_code=sandbox_exit_code,
        input_payload={"source": source, "input": input_payload},
        output_payload={
            "stdout": result.get("stdout"),
            "stderr": result.get("stderr"),
        },
        caller_owner_id=caller_owner_id,
        caller_key_id=caller_key_id,
        was_killed=was_killed,
        kill_reason=kill_reason,
    )


def create_router(
    *,
    limiter: Any,
    optional_api_key: Callable[..., Any],
) -> APIRouter:
    """Build the playground router.

    Why factory: ``limiter`` lives in part_001 and ``optional_api_key``
    lives in the auth shard — importing them at module-load creates a
    cycle. Same pattern as the public_integrations + admin_usage
    routers.
    """
    router = APIRouter()

    # See routes/public_integrations.py for the matching comment on why
    # the path lacks ``/api`` — the api_prefix_compat middleware in
    # part_001 strips the leading ``/api`` so this single registration
    # answers both ``/api/playground/test`` and ``/playground/test``.
    @router.post(
        "/playground/test",
        tags=["Playground"],
        summary=(
            "Run a buyer-supplied Python handler in a sandbox and return "
            "stdout / stderr / exit_code / execution_time_ms. Anonymous "
            "callable; IP-rate-limited at 5/minute."
        ),
    )
    @limiter.limit(_TEST_RATE_LIMIT)
    def playground_test(
        request: Request,
        body: dict[str, Any] = Body(...),
        caller: Any = Depends(optional_api_key),
    ) -> JSONResponse:
        if not _playground_enabled():
            raise _playground_disabled_error()
        source, input_payload, timeout_s = _validate_test_body(body)
        _run_listing_safety_or_block(source)
        started = time.monotonic()
        # The python_executor agent IS the sandbox. We synthesize a
        # call that wraps the buyer's `handler(payload)` definition
        # so they see the same execution semantics the platform uses
        # when their published agent gets called.
        # Bootstrap runs BEFORE the user source so we can install the
        # parsed input. The lookup for `handler` happens in the suffix,
        # AFTER the user's `def handler(payload)` has been defined.
        bootstrap = (
            "import json as _aztea_json, sys as _aztea_sys\n"
            f"_aztea_input = _aztea_json.loads({_input_payload_to_literal(input_payload)!r})\n"
        )
        suffix = (
            "\n"
            "try:\n"
            "    _aztea_handler = handler  # noqa: F821 — defined by buyer source above\n"
            "except NameError:\n"
            "    _aztea_sys.stderr.write('aztea-playground: define `def handler(payload): ...` and try again.\\n')\n"
            "    _aztea_sys.exit(2)\n"
            "try:\n"
            "    _aztea_result = _aztea_handler(_aztea_input)\n"
            "except Exception:\n"
            "    import traceback as _aztea_tb\n"
            "    _aztea_tb.print_exc()\n"
            "    _aztea_sys.exit(1)\n"
            "print(_aztea_json.dumps({'result': _aztea_result}))\n"
        )
        composed = bootstrap + source + suffix
        result = _python_executor.run({
            "code": composed,
            "timeout": timeout_s,
            "explain": False,
        })
        duration_ms = int((time.monotonic() - started) * 1000)
        caller_owner_id = caller.get("owner_id") if caller else None
        caller_key_id = caller.get("key_id") if caller else None
        execution_id = _record_test_invocation(
            source=source,
            input_payload=input_payload,
            result=result if isinstance(result, dict) else {},
            caller_owner_id=caller_owner_id,
            caller_key_id=caller_key_id,
            duration_ms=duration_ms,
        )
        return JSONResponse(
            content={
                "execution_id": execution_id,
                "execution_time_ms": duration_ms,
                "exit_code": (result or {}).get("exit_code", -1),
                "timed_out": bool((result or {}).get("timed_out")),
                "stdout": (result or {}).get("stdout") or "",
                "stderr": (result or {}).get("stderr") or "",
                "error": (result or {}).get("error"),
            }
        )

    # ── /api/playground/publish ─────────────────────────────────────────
    # Thin shim that points integrators at the canonical /skills endpoint.
    # The playground UI POSTs here as a single round-trip; for production
    # API integrators the canonical path is /skills which has the full
    # response shape and is documented in the OpenAPI spec.
    #
    # Why 308 (not a copy of the /skills logic): keeping a single source of
    # truth for the publish flow avoids two divergent listing-safety scans.
    # Browsers preserve the POST body on 308 by design. Curl users get a
    # one-line clarification in the response body for the rare case where
    # they hit /api/playground/publish directly.
    @router.post(
        "/playground/publish",
        tags=["Playground"],
        summary=(
            "Publish a SKILL.md / handler created in the browser playground. "
            "Delegates to POST /skills; same auth (worker scope) and same "
            "listing-safety pipeline."
        ),
    )
    def playground_publish(request: Request) -> RedirectResponse:
        if not _playground_enabled():
            raise _playground_disabled_error()
        # 308 preserves method + body. The api_prefix_compat middleware
        # then resolves /skills to the canonical handler in part_012.
        return RedirectResponse(url="/skills", status_code=308)

    return router


def _input_payload_to_literal(payload: Any) -> str:
    """Pure: stable JSON representation of input_payload suitable for
    embedding as a Python string literal in the bootstrap. Why split
    out: the f-string in ``playground_test`` would otherwise need
    manual escaping for nested quotes; centralising the conversion
    makes the bootstrap line readable."""
    import json as _json
    return _json.dumps(payload, ensure_ascii=False)
