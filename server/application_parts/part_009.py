# server.application shard 9 — async jobs routes: batch create + status,
# list, get, agent-scoped list, claim, heartbeat, release, complete, and the
# output verification decision endpoint. Uses the lease primitives from
# core.jobs and the settlement helpers from shard 5.


@app.post(
    "/jobs/batch",
    status_code=201,
    responses=_error_responses(400, 401, 402, 403, 422, 429, 500),
    tags=["Jobs"],
    summary="Create up to 50 jobs atomically. Single wallet pre-debit for total cost.",
)
@limiter.limit(_JOBS_CREATE_RATE_LIMIT)
def jobs_batch_create(
    request: Request,
    body: core_models.JobBatchCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    if not body.jobs:
        raise HTTPException(status_code=400, detail="jobs array must not be empty.")
    if len(body.jobs) > 50:
        raise HTTPException(status_code=400, detail="Batch size limited to 50 jobs.")

    caller_owner_id = _caller_owner_id(request)
    batch_id = str(uuid.uuid4())

    resolved: list[dict] = []
    total_price_cents = 0
    key_per_job_cap_cents = _caller_key_per_job_cap(caller)
    for spec in body.jobs:
        parent_job = _resolve_parent_job_for_creation(
            caller,
            spec.parent_job_id,
            parent_cascade_policy=spec.parent_cascade_policy,
        )
        parent_tree_depth = _to_non_negative_int((parent_job or {}).get("tree_depth"), default=0)
        tree_depth = parent_tree_depth + 1 if parent_job is not None else 0
        if tree_depth >= 10:
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.ORCHESTRATION_DEPTH_EXCEEDED,
                    "Maximum orchestration depth is 10 levels.",
                    {"max_depth": 10, "attempted_depth": tree_depth},
                ),
            )
        agent = registry.get_agent(spec.agent_id, include_unapproved=True)
        if agent is None or not _caller_can_access_agent(caller, agent):
            raise HTTPException(status_code=404, detail=f"Agent '{spec.agent_id}' not found.")
        _assert_agent_callable(spec.agent_id, agent)
        price_cents = _usd_to_cents(agent["price_per_call_usd"])
        if price_cents > 2000 and not _agent_has_verified_contract(agent):
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.VERIFIED_CONTRACT_REQUIRED,
                    "Jobs above $20 require a worker with a verified input/output contract.",
                    {"agent_id": agent["agent_id"], "price_cents": price_cents},
                ),
            )
        if key_per_job_cap_cents is not None and price_cents > key_per_job_cap_cents:
            raise HTTPException(
                status_code=402,
                detail=error_codes.make_error(
                    error_codes.SPEND_LIMIT_EXCEEDED,
                    "API key per-job cap exceeded.",
                    {
                        "scope": "api_key_per_job",
                        "key_id": str(caller.get("key_id") or "").strip() or None,
                        "limit_cents": key_per_job_cap_cents,
                        "attempted_cents": price_cents,
                        "agent_id": agent["agent_id"],
                    },
                ),
            )
        fee_bearer_policy = payments.normalize_fee_bearer_policy(spec.fee_bearer_policy)
        platform_fee_pct_at_create = int(payments.PLATFORM_FEE_PCT)
        success_distribution = payments.compute_success_distribution(
            price_cents,
            platform_fee_pct=platform_fee_pct_at_create,
            fee_bearer_policy=fee_bearer_policy,
        )
        caller_charge_cents = int(success_distribution["caller_charge_cents"])
        if spec.budget_cents is not None and price_cents > spec.budget_cents:
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.BUDGET_EXCEEDED,
                    f"Agent '{spec.agent_id}' price ({price_cents}¢) exceeds budget ({spec.budget_cents}¢).",
                    {"agent_id": spec.agent_id, "price_cents": price_cents, "budget_cents": spec.budget_cents},
                ),
            )
        try:
            normalized_spec_input_payload = _merge_protocol_input_envelope(
                spec.input_payload,
                input_artifacts=_normalize_protocol_artifact_list(
                    spec.input_artifacts,
                    field_name="jobs[].input_artifacts",
                ),
                preferred_input_formats=_normalize_format_preferences(
                    spec.preferred_input_formats,
                    field_name="jobs[].preferred_input_formats",
                ),
                preferred_output_formats=_normalize_format_preferences(
                    spec.preferred_output_formats,
                    field_name="jobs[].preferred_output_formats",
                ),
                communication_channel=_normalize_protocol_channel(
                    spec.communication_channel,
                    field_name="jobs[].communication_channel",
                ),
                protocol_metadata=_normalize_protocol_metadata(
                    spec.protocol_metadata,
                    field_name="jobs[].protocol_metadata",
                ),
                private_task=bool(spec.private_task),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        total_price_cents += caller_charge_cents
        resolved.append(
            {
                "agent": agent,
                "price_cents": price_cents,
                "caller_charge_cents": caller_charge_cents,
                "platform_fee_pct_at_create": platform_fee_pct_at_create,
                "fee_bearer_policy": fee_bearer_policy,
                "spec": spec,
                "input_payload": normalized_spec_input_payload,
                "parent_job_id": (parent_job or {}).get("job_id"),
                "tree_depth": tree_depth,
            }
        )

    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    if caller_wallet["balance_cents"] < total_price_cents:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.INSUFFICIENT_FUNDS,
                "Insufficient balance for batch.",
                {"balance_cents": caller_wallet["balance_cents"], "required_cents": total_price_cents},
            ),
        )

    created_jobs = []
    charge_tx_ids = []
    try:
        for item in resolved:
            agent = item["agent"]
            price_cents = item["price_cents"]
            caller_charge_cents = item["caller_charge_cents"]
            platform_fee_pct_at_create = item["platform_fee_pct_at_create"]
            fee_bearer_policy = item["fee_bearer_policy"]
            spec = item["spec"]
            input_payload = item["input_payload"]
            parent_job_id = item["parent_job_id"]
            tree_depth = item["tree_depth"]
            agent_wallet = payments.get_or_create_wallet(f"agent:{agent['agent_id']}")
            platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
            charge_tx_id = _pre_call_charge_or_402(
                caller=caller,
                caller_wallet_id=caller_wallet["wallet_id"],
                charge_cents=caller_charge_cents,
                agent_id=agent["agent_id"],
            )
            charge_tx_ids.append((caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent["agent_id"]))
            job = jobs.create_job(
                agent_id=agent["agent_id"],
                caller_owner_id=caller_owner_id,
                caller_wallet_id=caller_wallet["wallet_id"],
                agent_wallet_id=agent_wallet["wallet_id"],
                platform_wallet_id=platform_wallet["wallet_id"],
                price_cents=price_cents,
                caller_charge_cents=caller_charge_cents,
                platform_fee_pct_at_create=platform_fee_pct_at_create,
                fee_bearer_policy=fee_bearer_policy,
                charge_tx_id=charge_tx_id,
                input_payload=input_payload,
                agent_owner_id=agent.get("owner_id"),
                max_attempts=spec.max_attempts,
                parent_job_id=parent_job_id,
                tree_depth=tree_depth,
                parent_cascade_policy=spec.parent_cascade_policy,
                clarification_timeout_seconds=spec.clarification_timeout_seconds,
                clarification_timeout_policy=spec.clarification_timeout_policy,
                dispute_window_hours=spec.dispute_window_hours or _DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
                judge_agent_id=_extract_judge_agent_id(agent.get("input_schema")) or _QUALITY_JUDGE_AGENT_ID,
                callback_url=spec.callback_url or None,
                callback_secret=spec.callback_secret or None,
                output_verification_window_seconds=(
                    86400
                    if spec.output_verification_window_seconds is None
                    else spec.output_verification_window_seconds
                ),
                batch_id=batch_id,
            )
            _record_job_event(job, "job.created", actor_owner_id=caller["owner_id"])
            created_jobs.append(_job_response(job, caller))
    except HTTPException:
        for wallet_id, charge_tx_id, price_cents, agent_id in charge_tx_ids:
            try:
                payments.post_call_refund(wallet_id, charge_tx_id, price_cents, agent_id)
            except Exception as exc:
                _LOG.exception(
                    "Batch refund failed after handled error (wallet=%s charge_tx_id=%s agent=%s): %s",
                    wallet_id,
                    charge_tx_id,
                    agent_id,
                    exc,
                )
        raise
    except Exception:
        for wallet_id, charge_tx_id, price_cents, agent_id in charge_tx_ids:
            try:
                payments.post_call_refund(wallet_id, charge_tx_id, price_cents, agent_id)
            except Exception as exc:
                _LOG.exception(
                    "Batch refund failed after unhandled error (wallet=%s charge_tx_id=%s agent=%s): %s",
                    wallet_id,
                    charge_tx_id,
                    agent_id,
                    exc,
                )
        raise HTTPException(status_code=500, detail="Batch creation failed; all charges refunded.")

    return JSONResponse(
        content={
            "batch_id": batch_id,
            "jobs": created_jobs,
            "count": len(created_jobs),
            "total_price_cents": total_price_cents,
        },
        status_code=201,
    )


@app.get(
    "/jobs/batch/{batch_id}",
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Jobs"],
    summary="Get aggregate status for a batch created via POST /jobs/batch.",
)
@limiter.limit("60/minute")
def jobs_batch_status(
    request: Request,
    batch_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    owner_id = _caller_owner_id(request)
    batch_jobs = jobs.list_jobs_for_batch(batch_id, owner_id)
    if not batch_jobs:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found.")

    n_pending = 0
    n_running = 0
    n_awaiting_clarification = 0
    n_complete = 0
    n_failed = 0
    total_cost_cents = 0
    for job in batch_jobs:
        total_cost_cents += int(job.get("price_cents") or 0)
        status = str(job.get("status") or "")
        if status == "pending":
            n_pending += 1
        elif status == "running":
            n_running += 1
            n_pending += 1
        elif status == "awaiting_clarification":
            n_awaiting_clarification += 1
            n_pending += 1
        elif status == "complete":
            n_complete += 1
        elif status == "failed":
            n_failed += 1

    return JSONResponse(
        content={
            "batch_id": batch_id,
            "count": len(batch_jobs),
            "n_pending": n_pending,
            "n_running": n_running,
            "n_awaiting_clarification": n_awaiting_clarification,
            "n_complete": n_complete,
            "n_failed": n_failed,
            "total_cost_cents": total_cost_cents,
            "jobs": [_job_response(job, caller) for job in batch_jobs],
        }
    )


@app.get(
    "/jobs",
    response_model=core_models.JobsListResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def jobs_list(
    request: Request,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobsListResponse:
    _require_scope(caller, "caller")
    if status and status not in jobs.VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
    page_size = min(max(1, limit), 200)
    before_created_at, before_job_id = _decode_jobs_cursor(cursor)
    owner_id = _caller_owner_id(request)
    items = jobs.list_jobs_for_owner(
        owner_id,
        limit=page_size + 1,
        status=status,
        before_created_at=before_created_at,
        before_job_id=before_job_id,
    )
    next_cursor = None
    if len(items) > page_size:
        page_items = items[:page_size]
        last = page_items[-1]
        next_cursor = _encode_jobs_cursor(last["created_at"], last["job_id"])
    else:
        page_items = items
    return JSONResponse(
        content={
            "jobs": [_job_response(j, caller) for j in page_items],
            "next_cursor": next_cursor,
        }
    )


@app.get(
    "/jobs/{job_id}",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_get(
    request: Request,
    job_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "caller")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to view this job.")
    response = _job_response(job, caller)
    response["latest_message_id"] = jobs.get_latest_message_id(job_id)
    return JSONResponse(content=response)


@app.get(
    "/jobs/{job_id}/signature",
    include_in_schema=True,
    tags=["Identity"],
    summary="Public Ed25519 signature attesting which agent produced this job's output.",
    responses=_error_responses(404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_signature(request: Request, job_id: str) -> JSONResponse:
    """Public — anyone with a job_id can fetch the signature so they can
    independently verify the output via the agent's DID document."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    signature = job.get("output_signature")
    if not signature:
        raise HTTPException(
            status_code=404,
            detail="This job has no signature (not yet completed, or the agent has no signing key).",
        )
    agent_id = job.get("agent_id")
    base = (os.environ.get("SERVER_BASE_URL") or "").rstrip("/")
    verify_url = (
        f"{base}/agents/{agent_id}/did.json"
        if base and agent_id else None
    )
    return JSONResponse(content={
        "job_id": job_id,
        "agent_id": agent_id,
        "did": job.get("output_signed_by_did"),
        "alg": job.get("output_signature_alg") or "ed25519",
        "signature": signature,
        "signed_at": job.get("output_signed_at"),
        "verify_url": verify_url,
    })


@app.get(
    "/jobs/agent/{agent_id}",
    response_model=core_models.JobsListResponse,
    responses=_error_responses(401, 403, 404, 422, 429, 500),
)
@limiter.limit("60/minute")
def jobs_list_for_agent(
    request: Request,
    agent_id: str,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobsListResponse:
    _require_scope(caller, "worker")
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    if status and status not in jobs.VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
    page_size = min(max(1, limit), 200)
    before_created_at, before_job_id = _decode_jobs_cursor(cursor)
    items = jobs.list_jobs_for_agent(
        agent_id,
        limit=page_size + 1,
        status=status,
        before_created_at=before_created_at,
        before_job_id=before_job_id,
    )
    next_cursor = None
    if len(items) > page_size:
        page_items = items[:page_size]
        last = page_items[-1]
        next_cursor = _encode_jobs_cursor(last["created_at"], last["job_id"])
    else:
        page_items = items
    return JSONResponse(
        content={
            "jobs": [_job_response(j, caller) for j in page_items],
            "next_cursor": next_cursor,
        }
    )


@app.post(
    "/jobs/{job_id}/claim",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 429, 500),
)
@limiter.limit("60/minute")
def jobs_claim(
    request: Request,
    job_id: str,
    body: JobClaimRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    agent = registry.get_agent(str(job.get("agent_id") or ""), include_unapproved=True)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if (
        not _caller_is_admin(caller)
        and str(agent.get("review_status") or "approved").strip().lower() != "approved"
    ):
        raise HTTPException(status_code=403, detail="Agent listing is pending review and cannot accept jobs.")

    if not _caller_worker_authorized_for_job(caller, job):
        status = 403 if caller["type"] == "agent_key" else 409
        detail = "Not authorized for this agent job." if status == 403 else "Job is not claimable."
        raise HTTPException(status_code=status, detail=detail)
    worker_owner_id = caller["owner_id"]
    require_auth = caller["type"] == "user"
    claimed = jobs.claim_job(
        job_id,
        claim_owner_id=worker_owner_id,
        lease_seconds=body.lease_seconds,
        require_authorized_owner=require_auth,
    )
    if claimed is None:
        raise HTTPException(status_code=409, detail="Job is not claimable.")

    _record_job_event(
        claimed,
        "job.claimed",
        actor_owner_id=worker_owner_id,
        payload={
            "lease_seconds": body.lease_seconds,
            "attempt_count": claimed["attempt_count"],
        },
    )
    claimed["caller_owner_id"] = job.get("caller_owner_id")
    claimed["caller_trust_score"] = _caller_trust_score(str(job.get("caller_owner_id") or ""))
    return JSONResponse(content=_job_response(claimed, caller))


@app.post(
    "/jobs/{job_id}/heartbeat",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 410, 429, 500),
)
@limiter.limit("120/minute")
def jobs_heartbeat(
    request: Request,
    job_id: str,
    body: JobHeartbeatRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    worker_owner_id = caller["owner_id"]
    timed_out = _timeout_stale_lease_at_touchpoint(
        job,
        actor_owner_id=worker_owner_id,
        touchpoint="heartbeat",
    )
    if timed_out is not None:
        timed_out_response = _job_response(timed_out, caller)
        return JSONResponse(
            content=_timeout_error_payload(timed_out_response),
            status_code=410,
        )

    if caller["type"] != "master":
        _assert_worker_claim(job, caller, worker_owner_id, body.claim_token)

    heartbeat = jobs.heartbeat_job_lease(
        job_id,
        claim_owner_id=worker_owner_id,
        lease_seconds=body.lease_seconds,
        claim_token=body.claim_token,
        require_authorized_owner=(caller["type"] == "user"),
    )
    if heartbeat is None:
        raise HTTPException(status_code=409, detail="Unable to heartbeat this job claim.")

    _record_job_event(
        heartbeat,
        "job.heartbeat",
        actor_owner_id=worker_owner_id,
        payload={"lease_seconds": body.lease_seconds},
    )
    return JSONResponse(content=_job_response(heartbeat, caller))


@app.post(
    "/jobs/{job_id}/release",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 410, 429, 500),
)
@limiter.limit("60/minute")
def jobs_release(
    request: Request,
    job_id: str,
    body: JobReleaseRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    worker_owner_id = caller["owner_id"]
    timed_out = _timeout_stale_lease_at_touchpoint(
        job,
        actor_owner_id=worker_owner_id,
        touchpoint="release",
    )
    if timed_out is not None:
        timed_out_response = _job_response(timed_out, caller)
        return JSONResponse(
            content=_timeout_error_payload(timed_out_response),
            status_code=410,
        )

    if caller["type"] != "master":
        _assert_worker_claim(job, caller, worker_owner_id, body.claim_token)

    released = jobs.release_job_claim(
        job_id,
        claim_owner_id=worker_owner_id,
        claim_token=body.claim_token,
        require_authorized_owner=(caller["type"] == "user"),
    )
    if released is None:
        raise HTTPException(status_code=409, detail="Unable to release this job claim.")

    _record_job_event(
        released,
        "job.released",
        actor_owner_id=worker_owner_id,
        payload={},
    )
    return JSONResponse(content=_job_response(released, caller))


@app.post(
    "/jobs/{job_id}/complete",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 410, 422, 429, 500),
)
@limiter.limit("30/minute")
def jobs_complete(
    request: Request,
    job_id: str,
    body: JobCompleteRequest,
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
            touchpoint="complete",
        )
        if timed_out is not None:
            timed_out_response = _job_response(timed_out, caller)
            return (
                _timeout_error_payload(timed_out_response),
                410,
            )

        if job["settled_at"]:
            return _job_response(job, caller), 200
        if job["status"] == "complete" and job.get("completed_at"):
            settled = _settle_successful_job(job, actor_owner_id=actor_owner_id)
            return _job_response(settled, caller), 200

        _assert_settlement_claim_or_grace(
            job,
            caller=caller,
            claim_token=body.claim_token,
            action="complete",
        )
        try:
            normalized_output_payload = _merge_protocol_output_envelope(
                body.output_payload,
                output_artifacts=_normalize_protocol_artifact_list(
                    body.output_artifacts,
                    field_name="output_artifacts",
                ),
                output_format=(str(body.output_format).strip().lower() if body.output_format else None),
                protocol_metadata=_normalize_protocol_metadata(
                    body.protocol_metadata,
                    field_name="protocol_metadata",
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        agent = registry.get_agent(job["agent_id"], include_unapproved=True)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent '{job['agent_id']}' not found.")
        output_schema = agent.get("output_schema")
        if isinstance(output_schema, dict) and output_schema:
            mismatches = _validate_json_schema_subset(body.output_payload, output_schema)
            if mismatches:
                raise HTTPException(
                    status_code=422,
                    detail=error_codes.make_error(
                        error_codes.SCHEMA_MISMATCH,
                        "output_payload does not match the declared output_schema.",
                        {"mismatches": mismatches},
                    ),
                )

        quality = _run_quality_gate(job, agent, body.output_payload)
        jobs.set_job_quality_result(
            job_id,
            judge_verdict=quality["judge_verdict"],
            quality_score=quality["quality_score"],
            judge_agent_id=quality["judge_agent_id"],
        )
        if not quality["passed"]:
            failed = jobs.update_job_status(
                job_id,
                "failed",
                error_message=f"Quality judge failed: {quality['reason']}",
                completed=True,
            )
            if failed is None:
                raise HTTPException(status_code=409, detail="Unable to update job status.")
            settled_failed = _settle_failed_job(failed, actor_owner_id=actor_owner_id, event_type="job.failed_quality")
            return _job_response(settled_failed, caller), 200

        # Sign the output with the agent's private key, if it has one.
        # The signature attests *who* signed (the agent's DID), not that
        # the work is correct — quality verification is a separate concern.
        sig_b64: str | None = None
        sig_alg: str | None = None
        sig_did: str | None = None
        sig_at: str | None = None
        try:
            from core import crypto as _crypto

            private_pem = agent.get("signing_private_key")
            agent_did_value = agent.get("did")
            if private_pem and agent_did_value and normalized_output_payload is not None:
                sig_b64 = _crypto.sign_payload(private_pem, normalized_output_payload)
                sig_alg = str(agent.get("signing_alg") or "ed25519")
                sig_did = agent_did_value
                sig_at = datetime.now(timezone.utc).isoformat()
        except Exception:  # signing must never break completion
            _LOG.exception("Failed to sign output for job %s", job_id)
            sig_b64 = sig_alg = sig_did = sig_at = None

        updated = jobs.update_job_status(
            job_id,
            "complete",
            output_payload=normalized_output_payload,
            completed=True,
            output_signature=sig_b64,
            output_signature_alg=sig_alg,
            output_signed_by_did=sig_did,
            output_signed_at=sig_at,
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="Unable to update job status.")
        initialized = jobs.initialize_output_verification_state(job_id)
        if initialized is not None:
            updated = initialized
        _record_job_event(
            updated,
            "job.completed",
            actor_owner_id=actor_owner_id,
            payload={
                "status": updated["status"],
                "output_verification_status": updated.get("output_verification_status"),
                "output_verification_deadline_at": updated.get("output_verification_deadline_at"),
            },
        )
        settled = _settle_successful_job(updated, actor_owner_id=actor_owner_id)
        distribution = payments.compute_success_distribution(
            int(updated.get("price_cents") or 0),
            platform_fee_pct=updated.get("platform_fee_pct_at_create"),
            fee_bearer_policy=updated.get("fee_bearer_policy"),
        )
        platform_fee_cents = int(distribution["platform_fee_cents"])
        judge_fee_cents = min(_JUDGE_FEE_CENTS, platform_fee_cents)
        if judge_fee_cents > 0:
            judge_wallet = payments.get_or_create_wallet(f"agent:{quality['judge_agent_id']}")
            payments.record_judge_fee(
                updated["platform_wallet_id"],
                judge_wallet["wallet_id"],
                charge_tx_id=updated["charge_tx_id"],
                agent_id=updated["agent_id"],
                fee_cents=judge_fee_cents,
            )
            settled = jobs.get_job(job_id) or settled
        caller_email = _get_owner_email(settled.get("caller_owner_id", ""))
        if caller_email:
            _agent_row = registry.get_agent(settled.get("agent_id", ""))
            _agent_name = (_agent_row or {}).get("name", "agent")
            _email.send_job_complete(caller_email, job_id, _agent_name, int(settled.get("price_cents") or 0))
        _record_public_work_example(
            agent,
            settled.get("input_payload") or {},
            normalized_output_payload,
            job_id=job_id,
            latency_ms=_job_latency_ms(settled),
            quality_score=quality.get("quality_score"),
        )
        return _job_response(settled, caller), 200

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.complete:{job_id}",
        payload={
            "output_payload": body.output_payload,
            "output_artifacts": body.output_artifacts,
            "output_format": body.output_format,
            "protocol_metadata": body.protocol_metadata,
            "claim_token": body.claim_token,
        },
        operation=_operation,
    )


@app.post(
    "/jobs/{job_id}/verification",
    response_model=core_models.JobResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("30/minute")
def jobs_output_verification_decide(
    request: Request,
    job_id: str,
    body: JobVerificationDecisionRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "caller")

    def _operation() -> tuple[dict, int]:
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        if caller["type"] != "master" and caller["owner_id"] != job.get("caller_owner_id"):
            raise HTTPException(status_code=403, detail="Only the job caller can decide output verification.")
        if job.get("status") != "complete" or not job.get("completed_at"):
            raise HTTPException(status_code=400, detail="Output verification is only available for completed jobs.")
        if job.get("settled_at"):
            raise HTTPException(status_code=409, detail="Job is already settled.")

        initialized = jobs.initialize_output_verification_state(job_id) or job
        verification_status = _normalize_output_verification_status(initialized)
        if verification_status == "not_required":
            raise HTTPException(
                status_code=400,
                detail="This job does not have an output verification window configured.",
            )

        if verification_status == "pending":
            deadline = _parse_iso_datetime(initialized.get("output_verification_deadline_at"))
            if deadline is not None and datetime.now(timezone.utc) > deadline:
                expired = jobs.mark_output_verification_expired(
                    job_id,
                    decision_owner_id="system:verification-expiry-api",
                )
                if expired is not None:
                    initialized = expired
                    verification_status = "expired"
                    _record_job_event(
                        expired,
                        "job.output_verification_expired",
                        actor_owner_id=caller["owner_id"],
                        payload={"output_verification_deadline_at": expired.get("output_verification_deadline_at")},
                    )

        if body.decision == "accept":
            if disputes.has_dispute_for_job(job_id):
                raise HTTPException(status_code=409, detail="Cannot accept output after a dispute is already filed.")
            if verification_status == "accepted":
                settled = _settle_successful_job(
                    initialized,
                    actor_owner_id=caller["owner_id"],
                    require_dispute_window_expiry=False,
                )
                return _job_response(settled, caller), 200
            if verification_status in {"rejected", "expired"}:
                raise HTTPException(status_code=409, detail="Output verification decision is already closed for this job.")
            decided = jobs.set_output_verification_decision(
                job_id,
                decision="accept",
                decision_owner_id=caller["owner_id"],
                reason=body.reason,
            )
            if decided is None:
                raise HTTPException(status_code=409, detail="Unable to record output verification decision.")
            _record_job_event(
                decided,
                "job.output_verification_accepted",
                actor_owner_id=caller["owner_id"],
                payload={},
            )
            settled = _settle_successful_job(
                decided,
                actor_owner_id=caller["owner_id"],
                require_dispute_window_expiry=False,
            )
            return _job_response(settled, caller), 200

        if verification_status == "rejected":
            return _job_response(initialized, caller), 200
        if verification_status in {"accepted", "expired"}:
            raise HTTPException(status_code=409, detail="Output verification decision is already closed for this job.")

        rejection_reason = body.reason or "Caller rejected output during verification window."
        dispute_row = _ensure_output_rejection_dispute(
            initialized,
            filed_by_owner_id=caller["owner_id"],
            reason=rejection_reason,
            evidence=body.evidence,
        )
        decided = jobs.set_output_verification_decision(
            job_id,
            decision="reject",
            decision_owner_id=caller["owner_id"],
            reason=rejection_reason,
        )
        decided_job = decided or jobs.get_job(job_id) or initialized
        _record_job_event(
            decided_job,
            "job.output_verification_rejected",
            actor_owner_id=caller["owner_id"],
            payload={"dispute_id": dispute_row["dispute_id"]},
        )
        _record_job_event(
            decided_job,
            "job.dispute_filed",
            actor_owner_id=caller["owner_id"],
            payload={
                "dispute_id": dispute_row["dispute_id"],
                "side": "caller",
                "reason": rejection_reason,
                "auto_opened": True,
            },
        )
        return _job_response(decided_job, caller), 200

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.verification:{job_id}",
        payload=body.model_dump(),
        operation=_operation,
    )


