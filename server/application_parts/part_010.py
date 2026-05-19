
from core import db as _db
# server.application shard 10 — jobs fail/retry + typed messages + SSE
# stream + ratings (caller → agent, agent → caller) + disputes (get, file,
# trust-dispute management) + platform-level ops endpoints.

# Cap long-poll wait on GET /jobs/{id}/messages. 25s leaves margin under
# typical 30s proxy/idle timeouts so the response always lands client-side.
_JOB_MESSAGES_LONG_POLL_MAX_MS = 25_000


@app.post(
    "/jobs/{job_id}/fail",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 410, 429, 500),
)
@limiter.limit("30/minute")
def jobs_fail(
    request: Request,
    job_id: str,
    body: JobFailRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")

    def _operation() -> tuple[dict, int]:
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

        actor_owner_id = caller["owner_id"]
        if not _caller_worker_authorized_for_job(caller, job):
            raise HTTPException(
                status_code=403, detail="Not authorized for this agent job."
            )
        timed_out = _timeout_stale_lease_at_touchpoint(
            job,
            actor_owner_id=actor_owner_id,
            touchpoint="fail",
        )
        if timed_out is not None:
            timed_out_response = _job_response(timed_out, caller)
            return (
                _timeout_error_payload(timed_out_response),
                410,
            )

        refund_fraction = float(getattr(body, "refund_fraction", 1.0) or 1.0)

        if job["settled_at"]:
            return _job_response(job, caller), 200
        if job["status"] == "failed" and job.get("error_message") == body.error_message:
            settled = _settle_failed_job(
                job,
                actor_owner_id=actor_owner_id,
                event_type="job.failed",
                refund_fraction=refund_fraction,
            )
            return _job_response(settled, caller), 200

        _assert_settlement_claim_or_grace(
            job,
            caller=caller,
            claim_token=body.claim_token,
            action="fail",
        )

        updated = jobs.update_job_status(
            job_id, "failed", error_message=body.error_message, completed=True
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="Unable to update job status.")
        settled = _settle_failed_job(
            updated,
            actor_owner_id=actor_owner_id,
            event_type="job.failed",
            refund_fraction=refund_fraction,
        )
        return _job_response(settled, caller), 200

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.fail:{job_id}",
        payload={"error_message": body.error_message, "claim_token": body.claim_token},
        operation=_operation,
    )


@app.post(
    "/jobs/{job_id}/retry",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 422, 429, 500),
)
@limiter.limit("30/minute")
def jobs_retry(
    request: Request,
    job_id: str,
    body: JobRetryRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")

    def _operation() -> tuple[dict, int]:
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

        actor_owner_id = caller["owner_id"]
        require_auth = caller["type"] == "user"
        claim_owner_id = (
            actor_owner_id
            if require_auth
            else (job.get("claim_owner_id") or actor_owner_id)
        )
        if require_auth:
            _assert_worker_claim(job, caller, actor_owner_id, body.claim_token)

        try:
            updated = jobs.schedule_job_retry(
                job_id,
                retry_delay_seconds=body.retry_delay_seconds,
                error_message=body.error_message,
                claim_owner_id=claim_owner_id,
                claim_token=body.claim_token,
                require_authorized_owner=require_auth,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=_envelope_from_value_error(exc, "job"),
            )
        if updated is None:
            raise HTTPException(
                status_code=409, detail="Unable to schedule retry for this job."
            )

        if updated["status"] == "failed":
            settled = _settle_failed_job(
                updated, actor_owner_id=actor_owner_id, event_type="job.retry_exhausted"
            )
            return _job_response(settled, caller), 200

        _record_job_event(
            updated,
            "job.retry_scheduled",
            actor_owner_id=actor_owner_id,
            payload={
                "retry_delay_seconds": body.retry_delay_seconds,
                "retry_count": updated["retry_count"],
                "next_retry_at": updated["next_retry_at"],
            },
        )
        return _job_response(updated, caller), 200

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.retry:{job_id}",
        payload=body.model_dump(),
        operation=_operation,
    )


@app.post(
    "/jobs/{job_id}/messages",
    response_model=core_models.JobMessageResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_message_create(
    request: Request,
    job_id: str,
    body: JobMessageRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobMessageResponse:
    """
    Post a message to a job thread.

    Deprecated: the legacy free-form contract (`question`, `partial_result`,
    `clarification`, `clarification_needed`, `final_result`, `note`) remains
    accepted for one compatibility window. New integrations should use the
    typed protocol message shapes.
    """
    _require_any_scope(caller, "caller", "worker")
    job = jobs.get_job(job_id)
    # Return 403 in both "not found" and "not authorized" cases to prevent job-ID enumeration.
    if job is None or not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Job not found or not authorized.")

    raw_type = body.type
    raw_payload = dict(body.payload or {})
    raw_correlation_id = body.correlation_id

    # Steer goes through the dedicated /jobs/{id}/steer route. Without this
    # branch, the legacy JS aztea-cli (0.23.0 and older) — which posted
    # `{msg_type: 'steer', ...}` here — hit a 500 in the normalize/dispatch
    # path. After 1.6.2 the JS CLI is deprecated, but old installs in the
    # wild will keep arriving here for a while; surface a clean 400 with the
    # right redirect path so clients can migrate without an unhandled error.
    if str(raw_type or "").strip().lower() == "steer":
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                "jobs.messages.use_steer_endpoint",
                (
                    "Use POST /jobs/{job_id}/steer for steer messages. "
                    "The /messages endpoint does not accept type='steer'. "
                    "If you're seeing this from aztea-cli@npm, that package "
                    "is deprecated — run `pip install aztea` instead."
                ),
                {"redirect_path": f"/jobs/{job_id}/steer"},
            ),
        )

    # from_id is always derived from the authenticated caller — no impersonation allowed.
    if str(raw_type or "").strip().lower() == "agent_message":
        if body.channel is not None and "channel" not in raw_payload:
            raw_payload["channel"] = str(body.channel or "")[:64]
        if body.to_id is not None and "to_id" not in raw_payload:
            raw_payload["to_id"] = str(body.to_id or "")[:64]

    # Per-job message cap
    existing_count = jobs.count_job_messages(job_id)
    if existing_count >= 200:
        raise HTTPException(
            status_code=429,
            detail=error_codes.make_error(
                error_codes.RATE_LIMITED,
                "This job has reached the 200-message limit. No further messages can be added.",
                {"job_id": job_id, "max_messages": 200},
            ),
        )

    try:
        parsed = _normalize_job_message_protocol(
            raw_type,
            raw_payload,
            correlation_id=raw_correlation_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=_envelope_from_value_error(exc, "job"),
        )

    msg_type = parsed["type"]
    payload = parsed["payload"]

    # Enforce message payload size cap (16 KB)
    try:
        msg_payload_bytes = len(json.dumps(payload).encode("utf-8"))
    except Exception:
        msg_payload_bytes = 0
    if msg_payload_bytes > 16 * 1024:
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                error_codes.PAYLOAD_TOO_LARGE,
                "Message payload exceeds the 16 KB limit.",
                {"size_bytes": msg_payload_bytes, "limit_bytes": 16 * 1024},
            ),
        )

    # Clarification loop cap: max 20 unanswered clarification_requests per job
    if msg_type == "clarification_request":
        open_clarifications = jobs.count_open_clarification_requests(job_id)
        if open_clarifications >= 20:
            raise HTTPException(
                status_code=429,
                detail=error_codes.make_error(
                    error_codes.RATE_LIMITED,
                    "This job has too many unanswered clarification requests (max 20). "
                    "Respond to pending clarifications before sending more.",
                    {"job_id": job_id, "open_count": open_clarifications},
                ),
            )

    if caller["type"] == "master":
        from_id = f"agent:{job['agent_id']}"
    elif caller["owner_id"] == job["caller_owner_id"]:
        from_id = job["caller_owner_id"]
    else:
        from_id = caller["owner_id"]

    if msg_type == "tool_call":
        correlation_id = str(payload.get("correlation_id") or "").strip()
        if not correlation_id:
            payload["correlation_id"] = str(uuid.uuid4())
    elif msg_type == "tool_result":
        correlation_id = str(payload.get("correlation_id") or "").strip()
        if not correlation_id:
            raise HTTPException(
                status_code=400,
                detail="tool_result payload.correlation_id is required.",
            )
        if not _job_has_tool_call_correlation(job_id, correlation_id):
            raise HTTPException(
                status_code=400,
                detail=f"Unknown tool_result correlation_id '{correlation_id}'.",
            )

    try:
        msg = jobs.add_message(
            job_id,
            from_id,
            msg_type,
            payload,
            lease_seconds=_DEFAULT_LEASE_SECONDS,
        )
    except jobs.messaging.JobAlreadyTerminal as exc:
        # partial_output / steer racing a stop_when match. Surface as 409
        # so the caller can distinguish "you raced terminal" from generic
        # validation failures (400) and rate limits (429). 1.7.1 — error
        # code aligned to public spec (`job.invalid_state`); the prior
        # `job.terminal` slug doesn't appear in docs/errors.md.
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                "job.invalid_state",
                str(exc),
                {"job_id": job_id},
            ),
        ) from exc
    updated_job = jobs.get_job(job_id) or job
    _record_job_event(
        updated_job,
        "job.message_added",
        actor_owner_id=caller["owner_id"],
        payload={
            "type": msg_type,
            "message_id": msg["message_id"],
            "channel": payload.get("channel") if isinstance(payload, dict) else None,
            "to_id": payload.get("to_id") if isinstance(payload, dict) else None,
        },
    )

    return JSONResponse(content=msg, status_code=201)


def _extract_job_message_filters(
    *,
    msg_type: str | None = None,
    from_id: str | None = None,
    channel: str | None = None,
    to_id: str | None = None,
) -> dict[str, str | None]:
    normalized_type = str(msg_type or "").strip().lower() or None
    if (
        normalized_type is not None
        and normalized_type
        not in _TYPED_JOB_MESSAGE_TYPES.union(_LEGACY_JOB_MESSAGE_TYPES)
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported job message type filter: {normalized_type}",
        )
    normalized_from_id = str(from_id or "").strip() or None
    normalized_channel = str(channel or "").strip().lower() or None
    normalized_to_id = str(to_id or "").strip() or None
    return {
        "msg_type": normalized_type,
        "from_id": normalized_from_id,
        "channel": normalized_channel,
        "to_id": normalized_to_id,
    }


def _job_message_matches_filters(message: dict, filters: dict[str, str | None]) -> bool:
    expected_type = filters.get("msg_type")
    expected_from_id = filters.get("from_id")
    expected_channel = filters.get("channel")
    expected_to_id = filters.get("to_id")
    if (
        expected_type
        and str(message.get("type") or "").strip().lower() != expected_type
    ):
        return False
    if (
        expected_from_id
        and str(message.get("from_id") or "").strip() != expected_from_id
    ):
        return False
    payload = message.get("payload")
    if expected_channel or expected_to_id:
        if not isinstance(payload, dict):
            return False
        if (
            expected_channel
            and str(payload.get("channel") or "").strip().lower() != expected_channel
        ):
            return False
        if expected_to_id and str(payload.get("to_id") or "").strip() != expected_to_id:
            return False
    return True


@app.get(
    "/jobs/{job_id}/messages",
    response_model=core_models.JobMessagesResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
async def jobs_message_list(
    request: Request,
    job_id: str,
    since: int | None = None,
    msg_type: str | None = Query(default=None, alias="type"),
    from_id: str | None = None,
    channel: str | None = None,
    to_id: str | None = None,
    wait_ms: int = Query(default=0, ge=0, le=_JOB_MESSAGES_LONG_POLL_MAX_MS),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobMessagesResponse:
    job = jobs.get_job(job_id)
    # Return 403 in both "not found" and "not authorized" cases to prevent job-ID enumeration.
    if job is None or not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Job not found or not authorized.")
    filters = _extract_job_message_filters(
        msg_type=msg_type,
        from_id=from_id,
        channel=channel,
        to_id=to_id,
    )

    def _query() -> list:
        return jobs.get_messages(
            job_id,
            since_id=since,
            msg_type=filters["msg_type"],
            from_id=filters["from_id"],
            channel=filters["channel"],
            to_id=filters["to_id"],
        )

    items = _query()
    if items or wait_ms <= 0:
        return JSONResponse(content={"messages": items})
    # Long-poll: register a cross-thread waiter, sleep until signalled or timeout,
    # then re-run the query once. Always returns 200 (possibly empty on timeout).
    ev = jobs.register_message_waiter(job_id)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, ev.wait, wait_ms / 1000.0
        )
    finally:
        jobs.unregister_message_waiter(job_id, ev)
    return JSONResponse(content={"messages": _query()})


@app.get(
    "/jobs/{job_id}/stream",
    response_model=str,
    responses={
        200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
        **_error_responses(401, 403, 404, 429, 500),
    },
)
@limiter.limit("60/minute")
def jobs_message_stream(
    request: Request,
    job_id: str,
    since: int | None = None,
    msg_type: str | None = Query(default=None, alias="type"),
    from_id: str | None = None,
    channel: str | None = None,
    to_id: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> StreamingResponse:
    job = jobs.get_job(job_id)
    # Return 403 in both "not found" and "not authorized" cases to prevent job-ID enumeration.
    if job is None or not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Job not found or not authorized.")
    filters = _extract_job_message_filters(
        msg_type=msg_type,
        from_id=from_id,
        channel=channel,
        to_id=to_id,
    )

    def _iter_events():
        subscriber = _subscribe_job_stream(job_id)
        last_seen = since
        try:
            yield ": heartbeat\n\n"
            while True:
                batch = jobs.get_messages(
                    job_id,
                    since_id=last_seen,
                    limit=200,
                    msg_type=filters["msg_type"],
                    from_id=filters["from_id"],
                    channel=filters["channel"],
                    to_id=filters["to_id"],
                )
                if batch:
                    for item in batch:
                        if not _job_message_matches_filters(item, filters):
                            continue
                        message_id = int(item["message_id"])
                        if last_seen is not None and message_id <= last_seen:
                            continue
                        last_seen = message_id
                        yield _job_message_to_sse(item)
                    continue

                latest_job = jobs.get_job(job_id)
                if (
                    latest_job is None
                    or latest_job.get("status") in _JOB_TERMINAL_STATUSES
                ):
                    break

                try:
                    queued = subscriber.get(timeout=_JOB_STREAM_HEARTBEAT_SECONDS)
                except Empty:
                    yield ": heartbeat\n\n"
                    latest_job = jobs.get_job(job_id)
                    if (
                        latest_job is None
                        or latest_job.get("status") in _JOB_TERMINAL_STATUSES
                    ):
                        break
                    continue

                queued_id = int(queued.get("message_id") or 0)
                if last_seen is not None and queued_id <= last_seen:
                    continue
                if not _job_message_matches_filters(queued, filters):
                    continue
                last_seen = queued_id
                yield _job_message_to_sse(queued)
        finally:
            _unsubscribe_job_stream(job_id, subscriber)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        _iter_events(), media_type="text/event-stream", headers=headers
    )


_USER_EVENTS_HEARTBEAT_SECONDS = 25


@app.get(
    "/jobs/events",
    response_model=str,
    responses={
        200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
        **_error_responses(401, 429, 500),
    },
    summary="Real-time job event feed for the authenticated user",
)
@limiter.limit("10/minute")
def jobs_user_event_stream(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> StreamingResponse:
    """SSE feed that emits a job event object every time any of the caller's
    jobs change state (created, claimed, completed, failed, etc.).  The client
    can use this to keep the jobs list in sync without polling every 20 s.

    Each SSE data line is a JSON-encoded job_event row.  The stream stays open
    until the client disconnects; a comment heartbeat fires every 25 s so
    proxies don't close the connection.
    """
    owner_id: str = caller["owner_id"]
    owner_ids: list[str] = [owner_id, *_fold_in_master_owner_ids(caller)]

    def _iter_user_events():
        subscribers = [jobs.subscribe_user_job_events(oid) for oid in owner_ids]
        try:
            yield ": heartbeat\n\n"
            while True:
                event = None
                # Round-robin across all subscribed channels so master-folded
                # callers see events from both their own owner_id and master.
                for sub in subscribers:
                    try:
                        event = sub.get(timeout=_USER_EVENTS_HEARTBEAT_SECONDS / max(1, len(subscribers)))
                        break
                    except Empty:
                        continue
                if event is None:
                    yield ": heartbeat\n\n"
                    continue
                try:
                    data = json.dumps(event, default=str)
                except (TypeError, ValueError):
                    continue
                yield f"data: {data}\n\n"
        finally:
            for oid, sub in zip(owner_ids, subscribers):
                jobs.unsubscribe_user_job_events(oid, sub)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        _iter_user_events(), media_type="text/event-stream", headers=headers
    )


@app.post(
    "/auth/socket-token",
    responses=_error_responses(401, 429, 500, 503),
    summary="Mint a short-lived token for the Elixir realtime WebSocket",
)
@limiter.limit("60/minute")
def auth_socket_token(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Return a 5-minute HMAC token the frontend uses to open the Phoenix socket.

    The socket itself terminates in the Elixir sidecar and is reverse-proxied
    by Caddy under `/elixir/socket/*`. The token is signed with the same
    ELIXIR_INTERNAL_SHARED_SECRET that authenticates Python → Elixir POSTs, so
    one rotated env var invalidates both directions.

    Returns 503 when the secret isn't configured — clients should fall back to
    polling, which already works.
    """
    owner_id: str = caller["owner_id"]
    try:
        token_payload = _job_events.issue_socket_token(owner_id)
    except _job_events.SocketTokenError:
        raise HTTPException(
            status_code=503,
            detail="Realtime socket is not configured on this deployment.",
        )
    return JSONResponse(content=token_payload)


# ---------------------------------------------------------------------------
# Reputation + operations routes
# ---------------------------------------------------------------------------


_CLAWBACK_NOT_EVALUATED = "not_evaluated"
_CLAWBACK_NO_AGENT = "agent_unavailable"
_CLAWBACK_NOT_SETTLED = "settlement_pending"
_CLAWBACK_NO_CURVE = "no_payout_curve"
_CLAWBACK_TOP_RATING = "top_rating_no_clawback"


def _resolve_rating_clawback(
    agent: dict | None,
    job: dict,
    rating: int,
) -> dict[str, Any]:
    """Compute (and persist) the payout-curve clawback for a freshly-rated job.

    Returns a typed dict on every path so callers can distinguish "we
    evaluated and chose not to claw back" from "we never tried". The
    `applied` boolean is the single source of truth for whether ledger
    rows moved.
    """
    if not agent:
        return {"applied": False, "clawback_cents": 0, "reason": _CLAWBACK_NO_AGENT}
    if not job.get("settled_at"):
        return {
            "applied": False,
            "clawback_cents": 0,
            "reason": _CLAWBACK_NOT_SETTLED,
        }
    from core import payout_curve as _pc

    raw_curve = agent.get("payout_curve")
    curve = _pc.parse_curve(raw_curve) if raw_curve else None
    if not curve:
        return {"applied": False, "clawback_cents": 0, "reason": _CLAWBACK_NO_CURVE}

    fraction = _pc.fraction_for_rating(curve, rating)
    if fraction >= 1.0:
        return {
            "applied": False,
            "clawback_cents": 0,
            "payout_fraction": fraction,
            "reason": _CLAWBACK_TOP_RATING,
        }

    distribution = payments.compute_success_distribution(
        int(job.get("price_cents") or 0),
        platform_fee_pct=int(
            job.get("platform_fee_pct_at_create") or payments.PLATFORM_FEE_PCT
        ),
        fee_bearer_policy=str(job.get("fee_bearer_policy") or "caller"),
    )
    return _pc.apply_curve_clawback(
        job_id=str(job["job_id"]),
        agent_id=str(job["agent_id"]),
        agent_wallet_id=str(job["agent_wallet_id"]),
        caller_wallet_id=str(job["caller_wallet_id"]),
        agent_payout_cents=int(distribution["agent_payout_cents"]),
        payout_fraction=fraction,
    )


@app.post(
    "/jobs/{job_id}/rating",
    status_code=201,
    response_model=core_models.JobRatingResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("30/minute")
def jobs_rate(
    request: Request,
    job_id: str,
    body: JobRatingRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobRatingResponse:
    _require_scope(caller, "caller")

    def _operation() -> tuple[dict, int]:
        job = jobs.get_job(job_id)
        # Return 403 in both "not found" and "not authorized" cases to prevent job-ID enumeration.
        if job is None or (
            caller["type"] != "master" and job["caller_owner_id"] != caller["owner_id"]
        ):
            raise HTTPException(
                status_code=403, detail="Job not found or not authorized."
            )

        # Block rating on non-terminal jobs (prevents trust farming)
        if job["status"] not in ("complete", "verified"):
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.JOB_RATE_STATUS_INVALID,
                    f"You can only rate a completed job. This job is currently '{job['status']}'.",
                    {"status": job["status"], "job_id": job_id},
                ),
            )

        # Block self-rating (caller rates their own agent)
        agent = registry.get_agent(
            str(job.get("agent_id") or ""), include_unapproved=True
        )
        if agent and agent.get("owner_id") == caller["owner_id"]:
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.JOB_SELF_RATE,
                    "You can't rate a job on an agent you own.",
                    {"job_id": job_id},
                ),
            )

        if disputes.has_dispute_for_job(job_id):
            raise HTTPException(
                status_code=409, detail="Ratings are locked once a dispute is filed."
            )

        try:
            rating = reputation.record_job_quality_rating(
                job_id, caller["owner_id"], body.rating
            )
        except ValueError as exc:
            message = str(exc)
            if "already rated by a different caller" in message:
                # Defence-in-depth: should not be reachable from this endpoint
                # because the prior ownership check already returns 403, but a
                # direct call to the reputation helper from an admin path could
                # land here. Map to 403 with a precise envelope.
                raise HTTPException(
                    status_code=403,
                    detail=error_codes.make_error(
                        error_codes.JOB_ALREADY_RATED,
                        "This job was rated by a different caller.",
                        {"job_id": job_id},
                    ),
                )
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.JOB_INVALID_RATING,
                    message,
                    {"job_id": job_id},
                ),
            )
        # Trust scores feed the agents-list cache (TTL 15s, see part_007.py).
        # Without explicit invalidation, list_agents/search_specialists could
        # show a stale trust_score for up to 15s after a rating, while
        # `manage_job action=examples` (which hits the single-agent endpoint
        # and bypasses the list cache) would show the fresh value — exactly
        # the discrepancy reported in the 2026-05-18 audit.
        _invalidate_agents_list_cache()

        metrics = reputation.compute_trust_metrics(job["agent_id"])
        if body.rating == 5:
            five_star_count = reputation.count_caller_given_ratings(
                caller["owner_id"], rating=5
            )
            if five_star_count >= 10:
                milestone = five_star_count // 10
                payments.adjust_caller_trust_once(
                    caller["owner_id"],
                    delta=0.02,
                    reason="five_star_milestone",
                    related_id=f"milestone:{milestone}",
                )

        # Resolve clawback into a typed payload so callers can distinguish
        # "we evaluated and chose not to claw back" from "we never tried".
        # Pre-1.7.14 every branch but the inner happy-path returned plain
        # `None`, which the 2026-05-16 audit flagged as Bug #15 — every
        # rating response carried `clawback: null` and clients could not
        # tell why.
        clawback_result = _resolve_rating_clawback(agent, job, body.rating)

        _record_job_event(
            job,
            "job.rated",
            actor_owner_id=caller["owner_id"],
            payload={"rating": body.rating},
        )
        return {
            "rating": rating,
            "agent_reputation": metrics,
            "clawback": clawback_result,
        }, 201

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.rating:{job_id}",
        payload={"rating": body.rating},
        operation=_operation,
    )


@app.post(
    "/jobs/{job_id}/rate-caller",
    status_code=201,
    response_model=core_models.JobCallerRatingResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("30/minute")
def jobs_rate_caller(
    request: Request,
    job_id: str,
    body: JobRateCallerRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobCallerRatingResponse:
    _require_scope(caller, "worker")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_worker_authorized_for_job(caller, job):
        raise HTTPException(
            status_code=403, detail="Only the job's agent owner can rate the caller."
        )

    # Block rating on non-terminal jobs
    if job["status"] not in ("complete", "verified", "failed"):
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.JOB_RATE_STATUS_INVALID,
                f"You can only rate a caller after the job has finished. This job is currently '{job['status']}'.",
                {"status": job["status"], "job_id": job_id},
            ),
        )

    agent_owner_for_rating = (
        job["agent_owner_id"] if caller["type"] == "agent_key" else caller["owner_id"]
    )

    try:
        rating = reputation.record_caller_rating(
            job_id=job_id,
            agent_owner_id=agent_owner_for_rating,
            rating=body.rating,
            comment=body.comment,
        )
    except ValueError as exc:
        message = str(exc)
        if "already has a caller rating" in message:
            raise HTTPException(status_code=409, detail=message)
        raise HTTPException(status_code=400, detail=message)

    caller_reputation = reputation.compute_caller_trust_metrics(job["caller_owner_id"])
    # Persist the recomputed trust score back onto the caller's wallet so the
    # payout-curve clawback path (and any callers reading wallet.caller_trust)
    # see the fresh value. Without this, caller_trust stayed pinned at the 0.5
    # default forever — confirmed by the live audit on 2026-04-28.
    try:
        new_trust = caller_reputation.get("trust_score")
        if new_trust is not None:
            payments.update_wallet_caller_trust(
                job["caller_owner_id"], float(new_trust)
            )
    except (TypeError, ValueError):
        pass
    _record_job_event(
        job,
        "job.caller_rated",
        actor_owner_id=caller["owner_id"],
        payload={"rating": body.rating},
    )
    return JSONResponse(
        content={"rating": rating, "caller_reputation": caller_reputation},
        status_code=201,
    )


@app.get(
    "/jobs/{job_id}/dispute",
    response_model=core_models.DisputeResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_get_dispute(
    request: Request,
    job_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeResponse:
    """Fetch the dispute for a job, if one exists."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if caller["type"] != "master" and caller["owner_id"] not in (
        job.get("caller_owner_id"),
        job.get("agent_owner_id"),
    ):
        raise HTTPException(status_code=403, detail="Job not found or not authorized.")
    dispute_row = disputes.get_dispute_by_job(job_id)
    if dispute_row is None:
        raise HTTPException(status_code=404, detail="No dispute found for this job.")
    dispute_row["judgments"] = disputes.get_judgments(dispute_row["dispute_id"])
    return JSONResponse(content=_dispute_view(dispute_row))


@app.get(
    "/ops/platform-stats",
    tags=["Ops"],
    summary="Platform health and trust statistics. Requires admin scope.",
    responses=_error_responses(401, 403, 429, 500),
)
def ops_platform_stats(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    from datetime import datetime, timedelta, timezone

    since_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with jobs._conn() as conn:
        totals = conn.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = 'complete') AS total_completed,
              COUNT(*) FILTER (WHERE status = 'complete' AND created_at >= %s) AS completed_30d,
              SUM(CASE WHEN status = 'complete' THEN price_cents ELSE 0 END) AS total_value_cents
            FROM jobs
            """,
            (since_30d,),
        ).fetchone()
        dispute_stats = conn.execute(
            """
            SELECT
              COUNT(*) AS total_disputes,
              COUNT(*) FILTER (WHERE d.status IN ('final','resolved','consensus')) AS resolved_disputes,
              COUNT(*) FILTER (WHERE d.created_at >= %s) AS disputes_30d
            FROM disputes d
            JOIN jobs j ON j.job_id = d.job_id
            """,
            (since_30d,),
        ).fetchone()
        completed_30d_count = (
            conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = 'complete' AND created_at >= %s",
                (since_30d,),
            ).fetchone()["n"]
            or 1
        )
        latency_expr = (
            "EXTRACT(EPOCH FROM (completed_at::timestamptz - claimed_at::timestamptz))"
            if _db.IS_POSTGRES
            else "(julianday(completed_at) - julianday(claimed_at)) * 86400"
        )
        latency_rows = conn.execute(
            f"""
            SELECT {latency_expr} AS latency_s
            FROM jobs
            WHERE status = 'complete' AND claimed_at IS NOT NULL AND completed_at IS NOT NULL
              AND created_at >= %s
            ORDER BY latency_s
            """,
            (since_30d,),
        ).fetchall()
    agent_count = len(registry.get_agents(include_internal=False))
    lats = [float(r["latency_s"]) for r in latency_rows if r["latency_s"] is not None]
    if lats:
        mid = len(lats) // 2
        median_latency = round(
            lats[mid] if len(lats) % 2 else (lats[mid - 1] + lats[mid]) / 2, 2
        )
    else:
        median_latency = None
    total_completed = int((totals["total_completed"] or 0) if totals else 0)
    completed_30d = int((totals["completed_30d"] or 0) if totals else 0)
    total_value_cents = int((totals["total_value_cents"] or 0) if totals else 0)
    total_disputes = int((dispute_stats["total_disputes"] or 0) if dispute_stats else 0)
    resolved_disputes = int(
        (dispute_stats["resolved_disputes"] or 0) if dispute_stats else 0
    )
    disputes_30d = int((dispute_stats["disputes_30d"] or 0) if dispute_stats else 0)
    dispute_rate = (
        round(disputes_30d / completed_30d_count, 4) if completed_30d_count > 0 else 0.0
    )
    resolution_rate = (
        round(resolved_disputes / total_disputes, 4) if total_disputes > 0 else 1.0
    )
    return JSONResponse(
        content={
            "total_agents_registered": agent_count,
            "total_jobs_completed": total_completed,
            "total_jobs_last_30_days": completed_30d,
            "total_value_settled_cents": total_value_cents,
            "dispute_rate": dispute_rate,
            "dispute_resolution_rate": resolution_rate,
            "median_job_latency_seconds": median_latency,
            "platform_uptime_pct": 99.9,
        }
    )


@app.get(
    "/ops/disputes/{dispute_id}",
    response_model=core_models.DisputeResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def disputes_get(
    request: Request,
    dispute_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeResponse:
    """Fetch a dispute by its ID."""
    dispute_row = disputes.get_dispute(dispute_id)
    if dispute_row is None:
        raise HTTPException(
            status_code=404, detail=f"Dispute '{dispute_id}' not found."
        )
    if caller["type"] != "master":
        job = jobs.get_job(dispute_row["job_id"])
        owner_id = caller["owner_id"]
        if job and owner_id not in (
            job.get("caller_owner_id"),
            job.get("agent_owner_id"),
        ):
            raise HTTPException(status_code=403, detail="Not authorized.")
    dispute_row["judgments"] = disputes.get_judgments(dispute_id)
    return JSONResponse(content=_dispute_view(dispute_row))


# Public alias so the MCP `dispute_status` action can poll the dispute_id
# returned from `aztea_job(action='dispute')` directly. Same auth + ownership
# rules — `disputes_get` checks them inline.
app.add_api_route(
    "/disputes/{dispute_id}",
    disputes_get,
    methods=["GET"],
    response_model=core_models.DisputeResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)


@app.post(
    "/jobs/{job_id}/dispute",
    status_code=201,
    response_model=core_models.DisputeResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("20/minute")
def jobs_dispute(
    request: Request,
    job_id: str,
    body: JobDisputeRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeResponse:
    if not (_caller_has_scope(caller, "caller") or _caller_has_scope(caller, "worker")):
        raise HTTPException(
            status_code=403, detail="This endpoint requires caller or worker scope."
        )
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    # Ownership check (caller filed the job OR is the agent owner) happens
    # downstream in dispute creation. We split 404/403 here so the same
    # job_id returns the same status across status/cancel/dispute — the
    # prior /jobs/<bogus>/dispute → 403 while /jobs/<bogus>/status → 404
    # was the inconsistency the 2026-05-07 power-user eval flagged.
    # 1.7.9 — B-24 defense-in-depth. core/jobs/disputable.py rejects
    # status='cancelled' and the cancel-by-caller flavour of 'failed',
    # but the 1.7.7→1.7.8 evals kept reporting users filing disputes on
    # cancelled jobs and losing the 5¢ deposit. Add an explicit gate at
    # the top of the route so a dispute on a status-terminal-failure-or-
    # cancellation NEVER reaches the deposit-collection transaction, even
    # if disputable.is_disputable() drifts. The check is a strict superset
    # of the disputable check: any 'cancelled' or 'failed' job is rejected
    # before any deposit is touched. Failed jobs were already 100% refunded
    # via _settle_failed_job; filing a dispute would only lock 5¢ for the
    # judge run with nothing to claw back, which is the exact recourse-
    # trust violation the eval flagged.
    _current_status = str(job.get("status") or "").strip().lower()
    if _current_status in {"cancelled", "failed"}:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                "dispute.invalid_state",
                (
                    f"Cannot dispute a job in status '{_current_status}'. "
                    "Cancelled and failed jobs are already 100% refunded — "
                    "filing a dispute would lock your 5¢ deposit during the "
                    "judge run with no payout to claw back. If the agent's "
                    "output was actually wrong but the job ended up "
                    "cancelled-by-race, contact support@aztea.ai with the "
                    "job_id and we'll review manually."
                ),
                {
                    "current_status": _current_status,
                    "completed_at": job.get("completed_at"),
                },
            ),
        )
    # Single eligibility predicate (core/jobs/disputable.py). The 2026-05-08
    # eval found that the prior strict `status == "complete"` check rejected
    # disputes on receipts whose status had churned post-completion (sweepers,
    # verification rejections); the helper anchors on `completed_at` which is
    # set exactly once and never zeroed.
    deadline = _dispute_window_deadline(job)
    reason = disputable.is_disputable(
        job,
        deadline=deadline,
        has_existing_dispute=disputes.has_dispute_for_job(job_id),
        has_quality_rating=reputation.get_job_quality_rating(job_id) is not None,
    )
    if reason is not None:
        # 1.7.4 — surface the structured DisputeReason.code in the error
        # envelope. Pre-1.7.4 the route raised HTTPException with a plain
        # string detail, so FastAPI's default handler dropped the
        # `reason.code` (dispute.job_cancelled, dispute.window_expired,
        # dispute.not_completed, etc.) and the error key fell back to
        # request.invalid_input. Use error_codes.make_error() so the
        # canonical taxonomy reaches the client.
        raise HTTPException(
            status_code=reason.status_code,
            detail=error_codes.make_error(reason.code, reason.message),
        )

    side = _dispute_side_for_caller(caller, job)
    # Master keys are ops-grade — when they file a dispute they're acting on
    # behalf of the natural side of the job (caller by default). Substitute
    # the real owner_id so the dispute row, deposit, and clawback all attach
    # to the correct user wallet instead of the synthetic "master" id.
    if caller["type"] == "master":
        impersonated = (
            job.get("caller_owner_id") if side == "caller" else job.get("agent_owner_id")
        )
        if impersonated:
            caller = {**caller, "owner_id": impersonated}

    # Block self-disputes (caller and agent have same owner)
    if (
        job.get("caller_owner_id")
        and job.get("agent_owner_id")
        and job["caller_owner_id"] == job["agent_owner_id"]
    ):
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.JOB_SELF_DISPUTE,
                "You can't dispute a job on an agent you own.",
                {"job_id": job_id},
            ),
        )

    filing_deposit_cents = _compute_dispute_filing_deposit_cents(
        int(job.get("price_cents") or 0)
    )
    conn = payments._conn()
    lock_summary: dict[str, Any] = {}
    deposit_summary: dict[str, Any] = {}
    insufficient_phase = "dispute_create"
    _committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        created = disputes.create_dispute(
            job_id=job_id,
            filed_by_owner_id=caller["owner_id"],
            side=side,
            reason=body.reason,
            evidence=body.evidence,
            filing_deposit_cents=filing_deposit_cents,
            conn=conn,
        )
        insufficient_phase = "filing_deposit"
        deposit_summary = payments.collect_dispute_filing_deposit(
            created["dispute_id"],
            filed_by_owner_id=caller["owner_id"],
            amount_cents=filing_deposit_cents,
            conn=conn,
        )
        insufficient_phase = "clawback_lock"
        lock_summary = payments.lock_dispute_funds(created["dispute_id"], conn=conn)
        conn.execute("COMMIT")
        _committed = True
    except _db.IntegrityError:
        conn.execute("ROLLBACK")
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                "dispute.already_exists",
                "A dispute already exists for this job.",
                {"job_id": job_id},
            ),
        )
    except ValueError as exc:
        conn.execute("ROLLBACK")
        # F4 (red-team 2026-05-19): the new write-path eligibility check
        # in disputes.create_dispute raises ValueError prefixed with the
        # canonical error_code (e.g. "dispute.not_completed: ..."). Lift
        # the code out so the response carries a structured envelope
        # instead of a bare 400 with the raw message.
        raw_msg = str(exc)
        if raw_msg.startswith("dispute.not_completed"):
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    "dispute.not_completed",
                    "Disputes can only be filed for jobs that produced output.",
                    {"job_id": job_id, "current_status": job.get("status")},
                ),
            )
        if raw_msg.startswith("dispute.job_cancelled"):
            raise HTTPException(
                status_code=409,
                detail=error_codes.make_error(
                    "dispute.job_cancelled",
                    "Cancelled jobs are not disputable.",
                    {"job_id": job_id, "current_status": job.get("status")},
                ),
            )
        # Phase 2 (2026-05-19): every other ValueError from the dispute
        # write path now maps through the structured helper instead of
        # leaking str(exc) into the response.
        raise HTTPException(
            status_code=422,
            detail=_envelope_from_value_error(exc, "dispute"),
        )
    except PermissionError as exc:
        conn.execute("ROLLBACK")
        raise HTTPException(
            status_code=403,
            detail=_envelope_from_value_error(exc, "dispute"),
        )
    except payments.InsufficientBalanceError as exc:
        conn.execute("ROLLBACK")
        error_code = (
            error_codes.DISPUTE_FILING_DEPOSIT_INSUFFICIENT_BALANCE
            if insufficient_phase == "filing_deposit"
            else error_codes.DISPUTE_CLAWBACK_INSUFFICIENT_BALANCE
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": error_code,
                "balance_cents": exc.balance_cents,
                "required_cents": exc.required_cents,
            },
        )
    except Exception as exc:
        if not _committed:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        _LOG.exception("Unexpected error filing dispute for job %s", job_id)
        # Surface the underlying exception type so callers can act on it.
        # Without this, every dispute failure looks like "Failed to file dispute"
        # and there's no way for the client to distinguish a transient problem
        # (retry) from a permanent one (give up). Money flows are at stake here —
        # opaque errors are a recourse-trust violation.
        # Machine-readable taxonomy code (lowercase, dot-namespaced) so the
        # SDK + frontend table can map this to a warm sentence; the legacy
        # SCREAMING_CASE constant was free-form text, not an error code.
        detail = error_codes.make_error(
            error_codes.DISPUTE_FILING_FAILED,
            str(exc) or "Dispute could not be filed. Retry once; if it persists, contact support.",
            {
                "phase": insufficient_phase,
                "exception_type": type(exc).__name__,
                "job_id": job_id,
                "next_step": "Retry once. If it persists, contact support with this request_id.",
            },
        )
        raise HTTPException(status_code=500, detail=detail)
    _record_job_event(
        job,
        "job.dispute_filed",
        actor_owner_id=caller["owner_id"],
        payload={
            "dispute_id": created["dispute_id"],
            "side": side,
            "filing_deposit": deposit_summary,
            "lock": lock_summary,
        },
    )
    # Notify both parties about the dispute
    for _party_owner_id in {job.get("caller_owner_id"), job.get("agent_owner_id")}:
        _party_email = _get_owner_email(_party_owner_id or "")
        if _party_email:
            _email.send_dispute_opened(_party_email, job_id, created["dispute_id"])
    return JSONResponse(content=_dispute_view(created), status_code=201)


@app.post(
    "/jobs/{job_id}/dispute/operator-response",
    status_code=200,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("20/minute")
def jobs_dispute_operator_response(
    request: Request,
    job_id: str,
    body: dict,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Record the agent operator's defense for a dispute filed against them.

    2026-05-18 (D3): the caller filed a dispute; the operator now has a
    bounded window to respond. The response text is forwarded to the LLM
    judges alongside the caller's evidence so both sides are heard before
    any payout is moved. Silence past the deadline is an implicit waiver
    — the sweeper auto-advances the dispute to 'judging' on the caller's
    evidence alone (see ``disputes.expire_operator_response_windows``).
    """
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400, detail="JSON body with 'response_text' required."
        )
    response_text = str(body.get("response_text") or "").strip()
    if not response_text:
        raise HTTPException(
            status_code=400,
            detail="'response_text' is required (non-empty string).",
        )
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    dispute_row = disputes.get_dispute_for_job(job_id) if hasattr(
        disputes, "get_dispute_for_job"
    ) else None
    if dispute_row is None:
        # Fall back to a scan if the helper isn't there.
        for d in disputes.list_disputes(status="awaiting_operator", limit=500):
            if str(d.get("job_id") or "") == job_id:
                dispute_row = d
                break
    if dispute_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No dispute awaiting operator response for job '{job_id}'.",
        )
    try:
        updated = disputes.record_operator_response(
            dispute_row["dispute_id"],
            operator_owner_id=caller["owner_id"],
            response_text=response_text,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=_envelope_from_value_error(exc, "dispute"),
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail=_envelope_from_value_error(exc, "dispute"),
        )
    if updated is None:
        raise HTTPException(
            status_code=404, detail="Dispute disappeared during update."
        )
    _record_job_event(
        job, "job.dispute_operator_responded",
        actor_owner_id=caller["owner_id"],
        payload={"dispute_id": updated["dispute_id"]},
    )
    return JSONResponse(content=_dispute_view(updated))


@app.post(
    "/ops/disputes/{dispute_id}/judge",
    response_model=core_models.DisputeJudgeResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("30/minute")
def disputes_judge(
    request: Request,
    dispute_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeJudgeResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    if disputes.get_dispute(dispute_id) is None:
        raise HTTPException(
            status_code=404, detail=f"Dispute '{dispute_id}' not found."
        )
    try:
        dispute_payload, settlement = _resolve_dispute_with_judges(
            dispute_id, actor_owner_id=caller["owner_id"]
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=_envelope_from_value_error(exc, "dispute"),
        )
    except RuntimeError:
        _LOG.exception("Dispute judge execution failed for %s.", dispute_id)
        raise HTTPException(status_code=500, detail="Failed to resolve dispute.")
    return JSONResponse(content={"dispute": dispute_payload, "settlement": settlement})


@app.get(
    "/admin/disputes",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def admin_list_disputes(
    request: Request,
    limit: int = 200,
    status: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """List all disputes with job context and verdict summary, oldest first."""
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    capped_limit = max(1, min(int(limit), 500))
    rows = disputes.list_disputes(status=status or None, limit=capped_limit)
    result = []
    for d in rows:
        job = jobs.get_job(d["job_id"])
        judgments_list = disputes.get_judgments(d["dispute_id"])
        llm_judgments = [
            j for j in judgments_list if j.get("judge_kind") != "human_admin"
        ]
        if len(llm_judgments) >= 2:
            v0, v1 = llm_judgments[0]["verdict"], llm_judgments[1]["verdict"]
            verdict_summary = (
                f"Both agreed: {v0.replace('_', ' ')}"
                if v0 == v1
                else "Judges disagreed; needs ruling"
            )
        elif len(llm_judgments) == 1:
            verdict_summary = (
                f"1 judge: {llm_judgments[0]['verdict'].replace('_', ' ')}"
            )
        else:
            verdict_summary = "Awaiting judgment"
        result.append(
            {
                **d,
                "price_cents": int((job or {}).get("price_cents") or 0),
                "caller_owner_id": (job or {}).get("caller_owner_id"),
                "agent_owner_id": (job or {}).get("agent_owner_id"),
                "agent_id": (job or {}).get("agent_id"),
                "verdict_summary": verdict_summary,
                "judgment_count": len(judgments_list),
            }
        )
    result.sort(key=lambda x: x.get("filed_at") or "")
    return JSONResponse(content={"disputes": result, "total": len(result)})
