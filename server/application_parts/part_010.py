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
            raise HTTPException(status_code=403, detail="Not authorized for this agent job.")
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
        claim_owner_id = actor_owner_id if require_auth else (job.get("claim_owner_id") or actor_owner_id)
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
            raise HTTPException(status_code=422, detail=str(exc))
        if updated is None:
            raise HTTPException(status_code=409, detail="Unable to schedule retry for this job.")

        if updated["status"] == "failed":
            settled = _settle_failed_job(updated, actor_owner_id=actor_owner_id, event_type="job.retry_exhausted")
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
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to post to this job.")

    raw_type = body.type
    raw_payload = dict(body.payload or {})
    raw_correlation_id = body.correlation_id
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
        raise HTTPException(status_code=400, detail=str(exc))

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
            raise HTTPException(status_code=400, detail="tool_result payload.correlation_id is required.")
        if not _job_has_tool_call_correlation(job_id, correlation_id):
            raise HTTPException(
                status_code=400,
                detail=f"Unknown tool_result correlation_id '{correlation_id}'.",
            )

    msg = jobs.add_message(
        job_id,
        from_id,
        msg_type,
        payload,
        lease_seconds=_DEFAULT_LEASE_SECONDS,
    )
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
    if normalized_type is not None and normalized_type not in _TYPED_JOB_MESSAGE_TYPES.union(_LEGACY_JOB_MESSAGE_TYPES):
        raise HTTPException(status_code=400, detail=f"Unsupported job message type filter: {normalized_type}")
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
    if expected_type and str(message.get("type") or "").strip().lower() != expected_type:
        return False
    if expected_from_id and str(message.get("from_id") or "").strip() != expected_from_id:
        return False
    payload = message.get("payload")
    if expected_channel or expected_to_id:
        if not isinstance(payload, dict):
            return False
        if expected_channel and str(payload.get("channel") or "").strip().lower() != expected_channel:
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
def jobs_message_list(
    request: Request,
    job_id: str,
    since: int | None = None,
    msg_type: str | None = Query(default=None, alias="type"),
    from_id: str | None = None,
    channel: str | None = None,
    to_id: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobMessagesResponse:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to view messages.")
    filters = _extract_job_message_filters(
        msg_type=msg_type,
        from_id=from_id,
        channel=channel,
        to_id=to_id,
    )
    items = jobs.get_messages(
        job_id,
        since_id=since,
        msg_type=filters["msg_type"],
        from_id=filters["from_id"],
        channel=filters["channel"],
        to_id=filters["to_id"],
    )
    return JSONResponse(content={"messages": items})


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
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to view messages.")
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
                if latest_job is None or latest_job.get("status") in _JOB_TERMINAL_STATUSES:
                    break

                try:
                    queued = subscriber.get(timeout=_JOB_STREAM_HEARTBEAT_SECONDS)
                except Empty:
                    yield ": heartbeat\n\n"
                    latest_job = jobs.get_job(job_id)
                    if latest_job is None or latest_job.get("status") in _JOB_TERMINAL_STATUSES:
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
    return StreamingResponse(_iter_events(), media_type="text/event-stream", headers=headers)


# ---------------------------------------------------------------------------
# Reputation + operations routes
# ---------------------------------------------------------------------------

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
        if caller["type"] == "master":
            raise HTTPException(status_code=403, detail="Master key cannot submit quality ratings.")

        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        if job["caller_owner_id"] != caller["owner_id"]:
            raise HTTPException(status_code=403, detail="Only the original caller can rate this job.")

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
        agent = registry.get_agent(str(job.get("agent_id") or ""), include_unapproved=True)
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
            raise HTTPException(status_code=409, detail="Ratings are locked once a dispute is filed.")

        try:
            rating = reputation.record_job_quality_rating(job_id, caller["owner_id"], body.rating)
        except ValueError as exc:
            message = str(exc)
            if "already has a quality rating" in message:
                raise HTTPException(status_code=409, detail=message)
            raise HTTPException(status_code=400, detail=message)

        metrics = reputation.compute_trust_metrics(job["agent_id"])
        if body.rating == 5:
            five_star_count = reputation.count_caller_given_ratings(caller["owner_id"], rating=5)
            if five_star_count >= 10:
                milestone = five_star_count // 10
                payments.adjust_caller_trust_once(
                    caller["owner_id"],
                    delta=0.02,
                    reason="five_star_milestone",
                    related_id=f"milestone:{milestone}",
                )
        _record_job_event(
            job,
            "job.rated",
            actor_owner_id=caller["owner_id"],
            payload={"rating": body.rating},
        )
        return {"rating": rating, "agent_reputation": metrics}, 201

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
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Master key cannot submit caller ratings.")

    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_worker_authorized_for_job(caller, job):
        raise HTTPException(status_code=403, detail="Only the job's agent owner can rate the caller.")

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

    agent_owner_for_rating = job["agent_owner_id"] if caller["type"] == "agent_key" else caller["owner_id"]

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
    _record_job_event(
        job,
        "job.caller_rated",
        actor_owner_id=caller["owner_id"],
        payload={"rating": body.rating},
    )
    return JSONResponse(content={"rating": rating, "caller_reputation": caller_reputation}, status_code=201)


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
    if caller["type"] != "master":
        owner_id = caller["owner_id"]
        if owner_id not in (job.get("caller_owner_id"), job.get("agent_owner_id")):
            raise HTTPException(status_code=403, detail="Not authorized to view this dispute.")
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
    from datetime import datetime, timezone, timedelta
    since_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with jobs._conn() as conn:
        totals = conn.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = 'complete') AS total_completed,
              COUNT(*) FILTER (WHERE status = 'complete' AND created_at >= ?) AS completed_30d,
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
              COUNT(*) FILTER (WHERE d.created_at >= ?) AS disputes_30d
            FROM disputes d
            JOIN jobs j ON j.job_id = d.job_id
            """,
            (since_30d,),
        ).fetchone()
        completed_30d_count = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = 'complete' AND created_at >= ?",
            (since_30d,),
        ).fetchone()["n"] or 1
        latency_rows = conn.execute(
            """
            SELECT (julianday(completed_at) - julianday(claimed_at)) * 86400 AS latency_s
            FROM jobs
            WHERE status = 'complete' AND claimed_at IS NOT NULL AND completed_at IS NOT NULL
              AND created_at >= ?
            ORDER BY latency_s
            """,
            (since_30d,),
        ).fetchall()
    agent_count = len(registry.get_agents(include_internal=False))
    lats = [float(r["latency_s"]) for r in latency_rows if r["latency_s"] is not None]
    if lats:
        mid = len(lats) // 2
        median_latency = round(lats[mid] if len(lats) % 2 else (lats[mid - 1] + lats[mid]) / 2, 2)
    else:
        median_latency = None
    total_completed = int((totals["total_completed"] or 0) if totals else 0)
    completed_30d = int((totals["completed_30d"] or 0) if totals else 0)
    total_value_cents = int((totals["total_value_cents"] or 0) if totals else 0)
    total_disputes = int((dispute_stats["total_disputes"] or 0) if dispute_stats else 0)
    resolved_disputes = int((dispute_stats["resolved_disputes"] or 0) if dispute_stats else 0)
    disputes_30d = int((dispute_stats["disputes_30d"] or 0) if dispute_stats else 0)
    dispute_rate = round(disputes_30d / completed_30d_count, 4) if completed_30d_count > 0 else 0.0
    resolution_rate = round(resolved_disputes / total_disputes, 4) if total_disputes > 0 else 1.0
    return JSONResponse(content={
        "total_agents_registered": agent_count,
        "total_jobs_completed": total_completed,
        "total_jobs_last_30_days": completed_30d,
        "total_value_settled_cents": total_value_cents,
        "dispute_rate": dispute_rate,
        "dispute_resolution_rate": resolution_rate,
        "median_job_latency_seconds": median_latency,
        "platform_uptime_pct": 99.9,
    })


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
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
    if caller["type"] != "master":
        job = jobs.get_job(dispute_row["job_id"])
        owner_id = caller["owner_id"]
        if job and owner_id not in (job.get("caller_owner_id"), job.get("agent_owner_id")):
            raise HTTPException(status_code=403, detail="Not authorized.")
    dispute_row["judgments"] = disputes.get_judgments(dispute_id)
    return JSONResponse(content=_dispute_view(dispute_row))


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
        raise HTTPException(status_code=403, detail="This endpoint requires caller or worker scope.")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if job.get("status") != "complete" or not job.get("completed_at"):
        raise HTTPException(status_code=400, detail="Disputes can only be filed for completed jobs.")

    completed_at = _parse_iso_datetime(job.get("completed_at"))
    if completed_at is None:
        raise HTTPException(status_code=400, detail="Job completion timestamp is invalid.")
    deadline = _dispute_window_deadline(job)
    if deadline is None:
        raise HTTPException(status_code=400, detail="Job completion timestamp is invalid.")
    if datetime.now(timezone.utc) > deadline:
        raise HTTPException(status_code=400, detail="Dispute window has expired for this job.")

    side = _dispute_side_for_caller(caller, job)

    # Block self-disputes (caller and agent have same owner)
    if job.get("caller_owner_id") and job.get("agent_owner_id") and \
            job["caller_owner_id"] == job["agent_owner_id"]:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.JOB_SELF_DISPUTE,
                "You can't dispute a job on an agent you own.",
                {"job_id": job_id},
            ),
        )

    if reputation.get_job_quality_rating(job_id) is not None:
        raise HTTPException(status_code=409, detail="Disputes must be filed before the caller submits a rating.")
    if disputes.has_dispute_for_job(job_id):
        raise HTTPException(status_code=409, detail="A dispute already exists for this job.")

    filing_deposit_cents = _compute_dispute_filing_deposit_cents(int(job.get("price_cents") or 0))
    conn = payments._conn()
    lock_summary: dict[str, Any] = {}
    deposit_summary: dict[str, Any] = {}
    insufficient_phase = "dispute_create"
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
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=409, detail="A dispute already exists for this job.")
    except ValueError as exc:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=400, detail=str(exc))
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
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
    try:
        dispute_payload, settlement = _resolve_dispute_with_judges(dispute_id, actor_owner_id=caller["owner_id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
        llm_judgments = [j for j in judgments_list if j.get("judge_kind") != "human_admin"]
        if len(llm_judgments) >= 2:
            v0, v1 = llm_judgments[0]["verdict"], llm_judgments[1]["verdict"]
            verdict_summary = (
                f"Both agreed: {v0.replace('_', ' ')}"
                if v0 == v1
                else "Judges disagreed — needs ruling"
            )
        elif len(llm_judgments) == 1:
            verdict_summary = f"1 judge: {llm_judgments[0]['verdict'].replace('_', ' ')}"
        else:
            verdict_summary = "Awaiting judgment"
        result.append({
            **d,
            "price_cents": int((job or {}).get("price_cents") or 0),
            "caller_owner_id": (job or {}).get("caller_owner_id"),
            "agent_owner_id": (job or {}).get("agent_owner_id"),
            "agent_id": (job or {}).get("agent_id"),
            "verdict_summary": verdict_summary,
            "judgment_count": len(judgments_list),
        })
    result.sort(key=lambda x: x.get("filed_at") or "")
    return JSONResponse(content={"disputes": result, "total": len(result)})


