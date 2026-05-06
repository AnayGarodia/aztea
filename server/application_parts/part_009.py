# server.application shard 9 — async jobs routes: batch create + status,
# list, get, agent-scoped list, claim, heartbeat, release, complete, and the
# output verification decision endpoint. Uses the lease primitives from
# core.jobs and the settlement helpers from shard 5.

_COMPARE_SELECTION_WINDOW_SECONDS = 7 * 24 * 3600


def _batch_fee_split(job: dict) -> dict:
    price_cents = int(job.get("price_cents") or 0)
    caller_charge_cents = int(job.get("caller_charge_cents") or price_cents)
    fee_bearer_policy = payments.normalize_fee_bearer_policy(
        job.get("fee_bearer_policy") or "caller"
    )
    platform_fee_pct = int(
        job.get("platform_fee_pct_at_create") or payments.PLATFORM_FEE_PCT
    )
    distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct,
        fee_bearer_policy=fee_bearer_policy,
    )
    return {
        "fee_bearer_policy": fee_bearer_policy,
        "platform_fee_pct": platform_fee_pct,
        "caller_charge_cents": caller_charge_cents,
        "agent_payout_cents": int(distribution["agent_payout_cents"]),
        "platform_fee_cents": int(distribution["platform_fee_cents"]),
    }


def _batch_job_trace_item(
    job: dict, caller: core_models.CallerContext, *, include_detail: bool = False
) -> dict:
    """Compact per-job trace for caller agents narrating marketplace delegation."""
    agent = registry.get_agent(str(job.get("agent_id") or ""), include_unapproved=True)
    price_cents = int(job.get("caller_charge_cents") or job.get("price_cents") or 0)
    status = str(job.get("status") or "pending")
    receipt_status = "available" if status == "complete" else "pending"
    if status == "failed":
        receipt_status = "unavailable"
    item = {
        "job_id": job.get("job_id"),
        "agent_id": job.get("agent_id"),
        "agent_name": (agent or {}).get("name"),
        "agent_slug": (agent or {}).get("slug") or (agent or {}).get("agent_slug"),
        "status": status,
        "charge_cents": price_cents,
        "escrow": (
            "opened"
            if status in {"pending", "running", "awaiting_clarification"}
            else "closed"
        ),
        "settlement": (
            "settled"
            if status == "complete"
            else "refunded_or_failed"
            if status == "failed"
            else "pending"
        ),
        "fee_split": _batch_fee_split(job),
        "receipt": {
            "status": receipt_status,
            "verify_with": "aztea_job(action='verify', job_id=...)",
            "signature_endpoint": (
                f"/jobs/{job.get('job_id')}/signature"
                if status == "complete" and job.get("job_id")
                else None
            ),
        },
    }
    if include_detail:
        item["detail"] = _job_response(job, caller)
    # Inline a compact `output` payload on terminal jobs so callers don't have
    # to fan-out N follow-up status calls for each child of a batch. Truncate
    # at 6KB per job to keep the parent response sane; callers can still hit
    # /jobs/{id} for the full payload when they need it.
    if status == "complete" and job.get("output_payload") is not None:
        try:
            raw = json.dumps(job.get("output_payload"))
            if len(raw) <= 6000:
                item["output"] = job.get("output_payload")
            else:
                item["output_truncated"] = True
                item["output_preview"] = raw[:6000]
        except Exception:
            item["output"] = None
    elif status == "failed":
        item["error"] = job.get("error_message")
    return item


def _batch_parallel_trace(
    *,
    batch_id: str,
    batch_jobs: list[dict],
    caller: core_models.CallerContext,
    phase: str,
    intent: str | None = None,
    max_total_cents: int | None = None,
    include_detail: bool = False,
) -> dict:
    """Aggregate trace that makes parallel marketplace hiring visible to agents."""
    trace_jobs = [
        _batch_job_trace_item(job, caller, include_detail=include_detail)
        for job in batch_jobs
    ]
    counts = {
        "pending": 0,
        "running": 0,
        "awaiting_clarification": 0,
        "complete": 0,
        "failed": 0,
    }
    for job in batch_jobs:
        status = str(job.get("status") or "")
        if status in counts:
            counts[status] += 1
    total_charged_cents = sum(int(item.get("charge_cents") or 0) for item in trace_jobs)
    terminal_count = counts["complete"] + counts["failed"]
    return {
        "batch_id": batch_id,
        "phase": phase,
        "intent": intent,
        "market_role": "Aztea rails: discovery, escrow, receipts, settlement, recourse",
        "summary": (
            f"{len(trace_jobs)} specialist hires tracked in parallel; "
            f"{terminal_count}/{len(trace_jobs)} terminal."
        ),
        "total_charged_cents": total_charged_cents,
        "max_total_cents": max_total_cents,
        "within_cap": (
            True if max_total_cents is None else total_charged_cents <= max_total_cents
        ),
        "counts": counts,
        "jobs": trace_jobs,
        "marketplace_summary": {
            "rail": "jobs.batch",
            "escrow": "per_job",
            "settlement": "per_job_on_completion_or_refund",
            "receipt": "signed_per_completed_job",
        },
    }


def _compare_jobs_by_agent(compare_row: dict) -> tuple[list[dict], bool]:
    subjobs: list[dict] = []
    all_terminal = True
    agent_ids = compare_row.get("agent_ids") or []
    job_ids = compare_row.get("job_ids") or []
    for agent_id, job_id in zip(agent_ids, job_ids):
        job = jobs.get_job(job_id)
        if job is None:
            all_terminal = False
            subjobs.append(
                {"agent_id": agent_id, "job_id": job_id, "status": "missing"}
            )
            continue
        status = str(job.get("status") or "").strip().lower()
        if status not in {"complete", "failed"}:
            all_terminal = False
        subjobs.append(job)
    return subjobs, all_terminal


def _compare_response(compare_row: dict, caller: core_models.CallerContext) -> dict:
    subjobs, all_terminal = _compare_jobs_by_agent(compare_row)
    if (
        all_terminal
        and str(compare_row.get("status") or "").strip().lower() == "running"
    ):
        refreshed = compare.mark_complete(compare_row["compare_id"])
        if refreshed is not None:
            compare_row = refreshed
    ordered_jobs: list[dict] = []
    total_charged_cents = 0
    for item in subjobs:
        if "status" in item and item.get("status") == "missing":
            ordered_jobs.append(item)
            continue
        total_charged_cents += int(
            item.get("caller_charge_cents") or item.get("price_cents") or 0
        )
        ordered_jobs.append(_job_response(item, caller))
    return {
        "compare_id": compare_row["compare_id"],
        "status": compare_row.get("status"),
        "created_at": compare_row.get("created_at"),
        "completed_at": compare_row.get("completed_at"),
        "winner_agent_id": compare_row.get("winner_agent_id"),
        "participation_fee_cents": 0,
        "total_charged_cents": total_charged_cents,
        "selection_required": compare_row.get("winner_agent_id") is None
        and all_terminal,
        "jobs": ordered_jobs,
        "job_ids": compare_row.get("job_ids") or [],
        "agent_ids": compare_row.get("agent_ids") or [],
    }


@app.post(
    "/jobs/compare",
    status_code=201,
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 422, 429, 500),
    tags=["Jobs"],
    summary="Create a compare session across 2-3 agents with one shared input payload "
    "(field 'input_payload', or aliases 'task' / 'input').",
)
@limiter.limit(_JOBS_CREATE_RATE_LIMIT)
def jobs_compare_create(
    request: Request,
    body: dict[str, Any] = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    raw_agent_ids = body.get("agent_ids")
    if not isinstance(raw_agent_ids, list):
        raise HTTPException(status_code=400, detail="agent_ids must be an array.")
    agent_ids = [
        str(item or "").strip() for item in raw_agent_ids if str(item or "").strip()
    ]
    if len(agent_ids) < 2 or len(agent_ids) > 3:
        raise HTTPException(
            status_code=400, detail="agent_ids must contain 2 or 3 agent IDs."
        )
    if len(set(agent_ids)) != len(agent_ids):
        raise HTTPException(status_code=400, detail="agent_ids must be unique.")
    # Accept the canonical field name and the two natural aliases. Resolve to the first
    # dict-typed value present so a caller passing `task` (the SDK/CLI shorthand) is not
    # silently dropped — historical bug: child jobs received an empty payload and failed.
    input_payload: dict[str, Any] | None = None
    for candidate_field in ("input_payload", "task", "input"):
        candidate = body.get(candidate_field)
        if candidate is None:
            continue
        if not isinstance(candidate, dict):
            raise HTTPException(
                status_code=422,
                detail=f"'{candidate_field}' must be an object.",
            )
        if input_payload is None:
            input_payload = candidate
            continue
        # Merge subsequent aliases on top so the most specific (input_payload) wins,
        # but a caller who sent only `task` still gets full propagation.
        merged = dict(candidate)
        merged.update(input_payload)
        input_payload = merged
    if input_payload is None:
        input_payload = {}
    max_attempts = max(1, min(int(body.get("max_attempts") or 3), 10))
    private_task = bool(body.get("private_task"))
    caller_owner_id = _caller_owner_id(request)
    client_id = _request_client_id(request, body.get("client_id"))
    key_per_job_cap_cents = _caller_key_per_job_cap(caller)
    merged_input_payload = _merge_protocol_input_envelope(
        input_payload,
        private_task=private_task,
    )

    resolved: list[dict[str, Any]] = []
    total_charged_cents = 0
    for agent_id in agent_ids:
        agent = registry.get_agent(agent_id, include_unapproved=True)
        if agent is None or not _caller_can_access_agent(caller, agent):
            raise HTTPException(
                status_code=404, detail=f"Agent '{agent_id}' not found."
            )
        _assert_agent_callable(agent_id, agent)
        pricing_estimate = _estimate_variable_charge(
            agent=agent,
            payload=merged_input_payload,
            per_job_cap_cents=key_per_job_cap_cents,
        )
        if pricing_estimate.get("cap_violated"):
            violation = pricing_estimate["cap_violated"]
            raise HTTPException(
                status_code=402,
                detail=error_codes.make_error(
                    error_codes.SPEND_LIMIT_EXCEEDED,
                    "Variable-price estimate exceeds your API key's per-job cap.",
                    {
                        "scope": "api_key_per_job",
                        "limit_cents": violation["limit_cents"],
                        "attempted_cents": violation["price_cents"],
                        "pricing_model": pricing_estimate["pricing_model"],
                        "agent_id": agent_id,
                    },
                ),
            )
        price_cents = int(pricing_estimate["price_cents"])
        distribution = payments.compute_success_distribution(
            price_cents,
            platform_fee_pct=int(payments.PLATFORM_FEE_PCT),
            fee_bearer_policy="caller",
        )
        caller_charge_cents = int(distribution["caller_charge_cents"])
        total_charged_cents += caller_charge_cents
        resolved.append(
            {
                "agent": agent,
                "price_cents": price_cents,
                "caller_charge_cents": caller_charge_cents,
            }
        )

    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    if int(caller_wallet.get("balance_cents") or 0) < total_charged_cents:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.INSUFFICIENT_FUNDS,
                "Insufficient balance for compare session.",
                {
                    "balance_cents": caller_wallet["balance_cents"],
                    "required_cents": total_charged_cents,
                },
            ),
        )

    created_jobs: list[dict] = []
    charge_tx_ids: list[tuple[str, str, int, str]] = []
    try:
        for item in resolved:
            agent = item["agent"]
            price_cents = item["price_cents"]
            caller_charge_cents = item["caller_charge_cents"]
            agent_wallet = payments.get_or_create_wallet(f"agent:{agent['agent_id']}")
            platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
            charge_tx_id = _pre_call_charge_or_402(
                caller=caller,
                caller_wallet_id=caller_wallet["wallet_id"],
                charge_cents=caller_charge_cents,
                agent_id=agent["agent_id"],
            )
            charge_tx_ids.append(
                (
                    caller_wallet["wallet_id"],
                    charge_tx_id,
                    caller_charge_cents,
                    agent["agent_id"],
                )
            )
            job = jobs.create_job(
                agent_id=agent["agent_id"],
                caller_owner_id=caller_owner_id,
                caller_wallet_id=caller_wallet["wallet_id"],
                agent_wallet_id=agent_wallet["wallet_id"],
                platform_wallet_id=platform_wallet["wallet_id"],
                price_cents=price_cents,
                caller_charge_cents=caller_charge_cents,
                platform_fee_pct_at_create=int(payments.PLATFORM_FEE_PCT),
                fee_bearer_policy="caller",
                client_id=client_id,
                charge_tx_id=charge_tx_id,
                input_payload=merged_input_payload,
                agent_owner_id=agent.get("owner_id"),
                max_attempts=max_attempts,
                dispute_window_hours=_DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
                judge_agent_id=_extract_judge_agent_id(agent.get("input_schema"))
                or _QUALITY_JUDGE_AGENT_ID,
                output_verification_window_seconds=_COMPARE_SELECTION_WINDOW_SECONDS,
            )
            _record_job_event(
                job,
                "job.created",
                actor_owner_id=caller["owner_id"],
                payload={"source": "jobs.compare", "max_attempts": max_attempts},
            )
            created_jobs.append(job)
    except Exception:
        for wallet_id, charge_tx_id, refund_cents, compare_agent_id in charge_tx_ids:
            try:
                payments.post_call_refund(
                    wallet_id, charge_tx_id, refund_cents, compare_agent_id
                )
            except Exception as exc:
                _LOG.exception(
                    "Compare-session refund failed after create error (wallet=%s charge_tx_id=%s agent=%s): %s",
                    wallet_id,
                    charge_tx_id,
                    compare_agent_id,
                    exc,
                )
        for created_job in created_jobs:
            failed = jobs.update_job_status(
                created_job["job_id"],
                "failed",
                error_message="Compare session creation failed before the session could be initialized.",
                completed=True,
            )
            if failed is not None and not failed.get("settled_at"):
                jobs.mark_settled(created_job["job_id"])
        raise

    try:
        compare_row = compare.create_compare(
            caller_owner_id,
            agent_ids,
            merged_input_payload,
            job_ids=[job["job_id"] for job in created_jobs],
        )
    except Exception:
        for wallet_id, charge_tx_id, refund_cents, compare_agent_id in charge_tx_ids:
            try:
                payments.post_call_refund(
                    wallet_id, charge_tx_id, refund_cents, compare_agent_id
                )
            except Exception as exc:
                _LOG.exception(
                    "Compare-session refund failed after compare-row create error (wallet=%s charge_tx_id=%s agent=%s): %s",
                    wallet_id,
                    charge_tx_id,
                    compare_agent_id,
                    exc,
                )
        for created_job in created_jobs:
            failed = jobs.update_job_status(
                created_job["job_id"],
                "failed",
                error_message="Compare session metadata creation failed.",
                completed=True,
            )
            if failed is not None and not failed.get("settled_at"):
                jobs.mark_settled(created_job["job_id"])
        raise
    response = _compare_response(compare_row, caller)
    response["total_charged_cents"] = total_charged_cents
    response["note"] = (
        "All agent charges are held now. Select a winner later to release payment only for that job."
    )
    return JSONResponse(content=response, status_code=201)


@app.get(
    "/jobs/compare/{compare_id}",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Jobs"],
    summary="Get compare-session status and sub-job results.",
)
@limiter.limit("60/minute")
def jobs_compare_get(
    request: Request,
    compare_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    compare_row = compare.get_compare(compare_id)
    if compare_row is None:
        raise HTTPException(
            status_code=404, detail=f"Compare session '{compare_id}' not found."
        )
    if caller["type"] != "master" and caller["owner_id"] != compare_row.get(
        "caller_owner_id"
    ):
        raise HTTPException(
            status_code=403, detail="Not authorized to view this compare session."
        )
    return JSONResponse(content=_compare_response(compare_row, caller))


@app.post(
    "/jobs/compare/{compare_id}/select",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
    tags=["Jobs"],
    summary="Select the winner from a completed compare session and settle only that job.",
)
@limiter.limit("30/minute")
def jobs_compare_select(
    request: Request,
    compare_id: str,
    body: dict[str, Any] = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    compare_row = compare.get_compare(compare_id)
    if compare_row is None:
        raise HTTPException(
            status_code=404, detail=f"Compare session '{compare_id}' not found."
        )
    if caller["type"] != "master" and caller["owner_id"] != compare_row.get(
        "caller_owner_id"
    ):
        raise HTTPException(
            status_code=403, detail="Not authorized to manage this compare session."
        )
    winner_agent_id = str(body.get("winner_agent_id") or "").strip()
    if not winner_agent_id:
        raise HTTPException(status_code=400, detail="winner_agent_id is required.")
    if winner_agent_id not in set(compare_row.get("agent_ids") or []):
        raise HTTPException(
            status_code=400,
            detail="winner_agent_id is not part of this compare session.",
        )

    subjobs, all_terminal = _compare_jobs_by_agent(compare_row)
    if not all_terminal:
        raise HTTPException(status_code=409, detail="Compare session is still running.")

    jobs_by_agent = {
        str(job.get("agent_id") or ""): job
        for job in subjobs
        if isinstance(job, dict) and job.get("job_id")
    }
    winner_job = jobs_by_agent.get(winner_agent_id)
    if winner_job is None or str(winner_job.get("status") or "") != "complete":
        raise HTTPException(
            status_code=409, detail="winner_agent_id must refer to a completed job."
        )

    try:
        selected = compare.select_winner(compare_id, winner_agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if selected is None:
        raise HTTPException(
            status_code=404, detail=f"Compare session '{compare_id}' not found."
        )

    refunded_job_ids: list[str] = []
    for agent_id in compare_row.get("agent_ids") or []:
        job = jobs_by_agent.get(agent_id)
        if job is None:
            continue
        if agent_id == winner_agent_id:
            initialized = (
                jobs.initialize_output_verification_state(job["job_id"]) or job
            )
            if not initialized.get("settled_at"):
                if (
                    str(initialized.get("output_verification_status") or "")
                    == "pending"
                ):
                    initialized = (
                        jobs.set_output_verification_decision(
                            job["job_id"],
                            decision="accept",
                            decision_owner_id=caller["owner_id"],
                            reason=f"Compare winner for session {compare_id}.",
                        )
                        or initialized
                    )
                settled = _settle_successful_job(
                    initialized,
                    actor_owner_id=caller["owner_id"],
                    require_dispute_window_expiry=False,
                )
                _record_job_event(
                    settled,
                    "job.compare_winner_selected",
                    actor_owner_id=caller["owner_id"],
                    payload={"compare_id": compare_id},
                )
            continue
        if str(job.get("status") or "") != "complete":
            continue
        initialized = jobs.initialize_output_verification_state(job["job_id"]) or job
        if initialized.get("settled_at"):
            continue
        if str(initialized.get("output_verification_status") or "") == "pending":
            initialized = (
                jobs.set_output_verification_decision(
                    job["job_id"],
                    decision="reject",
                    decision_owner_id=caller["owner_id"],
                    reason=f"Non-winning compare result for session {compare_id}.",
                )
                or initialized
            )
        payments.post_call_refund(
            initialized["caller_wallet_id"],
            initialized["charge_tx_id"],
            int(
                initialized.get("caller_charge_cents")
                or initialized.get("price_cents")
                or 0
            ),
            initialized["agent_id"],
        )
        jobs.mark_settled(initialized["job_id"])
        refreshed = jobs.get_job(initialized["job_id"]) or initialized
        _record_job_event(
            refreshed,
            "job.compare_non_winner_refunded",
            actor_owner_id=caller["owner_id"],
            payload={"compare_id": compare_id},
        )
        refunded_job_ids.append(refreshed["job_id"])

    response = _compare_response(selected, caller)
    response["winner_agent_id"] = winner_agent_id
    response["refunded_job_ids"] = refunded_job_ids
    response["note"] = (
        "Winner settled. Non-winning completed jobs were refunded in full to the caller."
    )
    return JSONResponse(content=response)


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

    # Defense-in-depth: accept ?dry_run=true as a query param so older
    # MCP/SDK clients that don't yet forward the body field can still ask
    # for an estimate without burning escrow.
    qp_dry_run = str(request.query_params.get("dry_run") or "").strip().lower()
    if qp_dry_run in {"1", "true", "yes"}:
        body.dry_run = True

    caller_owner_id = _caller_owner_id(request)
    request_client_id = _request_client_id(request)
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
        parent_tree_depth = _to_non_negative_int(
            (parent_job or {}).get("tree_depth"), default=0
        )
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
            raise HTTPException(
                status_code=404, detail=f"Agent '{spec.agent_id}' not found."
            )
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
                    {
                        "agent_id": spec.agent_id,
                        "price_cents": price_cents,
                        "budget_cents": spec.budget_cents,
                    },
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
                "success_distribution": success_distribution,
                "client_id": _request_client_id(request, spec.client_id)
                or request_client_id,
                "spec": spec,
                "input_payload": normalized_spec_input_payload,
                "parent_job_id": (parent_job or {}).get("job_id"),
                "tree_depth": tree_depth,
            }
        )

    if body.dry_run:
        planned_jobs: list[dict[str, Any]] = []
        for index, item in enumerate(resolved):
            agent = item["agent"]
            distribution = item["success_distribution"]
            planned_jobs.append(
                {
                    "index": index,
                    "agent_id": agent["agent_id"],
                    "agent_slug": agent.get("slug") or agent.get("agent_slug"),
                    "agent_name": agent.get("name"),
                    "price_cents": int(item["price_cents"]),
                    "caller_charge_cents": int(item["caller_charge_cents"]),
                    "fee_split": {
                        "fee_bearer_policy": item["fee_bearer_policy"],
                        "platform_fee_pct": int(item["platform_fee_pct_at_create"]),
                        "agent_payout_cents": int(distribution["agent_payout_cents"]),
                        "platform_fee_cents": int(distribution["platform_fee_cents"]),
                    },
                }
            )
        within_cap = (
            True
            if body.max_total_cents is None
            else total_price_cents <= body.max_total_cents
        )
        return JSONResponse(
            content={
                "mode": "parallel_marketplace_hire_estimate",
                "charge_status": "not_charged",
                "batch_id": None,
                "intent": body.intent,
                "job_count": len(planned_jobs),
                "estimated_total_charged_cents": total_price_cents,
                "max_total_cents": body.max_total_cents,
                "within_cap": within_cap,
                "planned_jobs": planned_jobs,
                "marketplace_summary": {
                    "rail": "jobs.batch",
                    "escrow": "would_open_per_job",
                    "settlement": "would_settle_or_refund_per_job",
                    "receipt": "signed_per_completed_job",
                    "charge_status": "not_charged",
                },
                "next_step": (
                    "Re-call with dry_run=false to submit this batch."
                    if within_cap
                    else "Raise max_total_cents or remove jobs before submitting."
                ),
            }
        )

    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    if body.max_total_cents is not None and total_price_cents > body.max_total_cents:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.BUDGET_EXCEEDED,
                "Batch total exceeds max_total_cents.",
                {
                    "max_total_cents": body.max_total_cents,
                    "attempted_cents": total_price_cents,
                    "job_count": len(body.jobs),
                },
            ),
        )
    if caller_wallet["balance_cents"] < total_price_cents:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.INSUFFICIENT_FUNDS,
                "Insufficient balance for batch.",
                {
                    "balance_cents": caller_wallet["balance_cents"],
                    "required_cents": total_price_cents,
                },
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
            client_id = item["client_id"]
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
            charge_tx_ids.append(
                (
                    caller_wallet["wallet_id"],
                    charge_tx_id,
                    caller_charge_cents,
                    agent["agent_id"],
                )
            )
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
                client_id=client_id,
                charge_tx_id=charge_tx_id,
                input_payload=input_payload,
                agent_owner_id=agent.get("owner_id"),
                max_attempts=spec.max_attempts,
                parent_job_id=parent_job_id,
                tree_depth=tree_depth,
                parent_cascade_policy=spec.parent_cascade_policy,
                clarification_timeout_seconds=spec.clarification_timeout_seconds,
                clarification_timeout_policy=spec.clarification_timeout_policy,
                dispute_window_hours=spec.dispute_window_hours
                or _DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
                judge_agent_id=_extract_judge_agent_id(agent.get("input_schema"))
                or _QUALITY_JUDGE_AGENT_ID,
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
                payments.post_call_refund(
                    wallet_id, charge_tx_id, price_cents, agent_id
                )
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
                payments.post_call_refund(
                    wallet_id, charge_tx_id, price_cents, agent_id
                )
            except Exception as exc:
                _LOG.exception(
                    "Batch refund failed after unhandled error (wallet=%s charge_tx_id=%s agent=%s): %s",
                    wallet_id,
                    charge_tx_id,
                    agent_id,
                    exc,
                )
        raise HTTPException(
            status_code=500, detail="Batch creation failed; all charges refunded."
        )

    trace = _batch_parallel_trace(
        batch_id=batch_id,
        batch_jobs=[
            jobs.get_job(job["job_id"]) or job
            for job in created_jobs
            if isinstance(job, dict) and job.get("job_id")
        ],
        caller=caller,
        phase="submitted",
        intent=body.intent,
        max_total_cents=body.max_total_cents,
    )
    return JSONResponse(
        content={
            "batch_id": batch_id,
            "jobs": created_jobs,
            "count": len(created_jobs),
            "total_price_cents": total_price_cents,
            "total_charged_cents": total_price_cents,
            "job_ids": [
                job.get("job_id")
                for job in created_jobs
                if isinstance(job, dict) and job.get("job_id")
            ],
            "mode": "parallel_marketplace_hire",
            "intent": body.intent,
            "max_total_cents": body.max_total_cents,
            "marketplace_transaction": {
                "status": "escrow_opened",
                "rail": "jobs.batch",
                "escrow": "opened_per_job",
                "settlement": "per_job_on_completion_or_refund",
                "receipt": "signed_per_completed_job",
            },
            "parallel_hire_trace": trace,
            "next_step": (
                f"Poll /jobs/batch/{batch_id} or aztea_workflow(action='batch_status', "
                f"batch_id='{batch_id}') to watch the parallel specialist hires settle."
            ),
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
    include_mode = str(request.query_params.get("include") or "full").strip().lower()
    compact = include_mode in {"minimal", "compact"}

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

    trace = _batch_parallel_trace(
        batch_id=batch_id,
        batch_jobs=batch_jobs,
        caller=caller,
        phase="status",
        include_detail=not compact,
    )
    response_jobs = (
        trace["jobs"] if compact else [_job_response(job, caller) for job in batch_jobs]
    )
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
            "total_charged_cents": sum(
                int(job.get("caller_charge_cents") or job.get("price_cents") or 0)
                for job in batch_jobs
            ),
            "jobs": response_jobs,
            "job_ids": [job.get("job_id") for job in batch_jobs if job.get("job_id")],
            "mode": "parallel_marketplace_hire",
            "marketplace_transaction": {
                "status": (
                    "complete"
                    if n_complete + n_failed == len(batch_jobs)
                    else "in_progress"
                ),
                "rail": "jobs.batch",
                "escrow": "per_job",
                "settlement": "settled_or_refunded_per_job",
                "receipt": "signed_per_completed_job",
            },
            "parallel_hire_trace": trace,
            "next_step": (
                "Summarize completed specialist outputs, call aztea_job(action='verify', job_id=...) "
                "for completed receipts when provenance matters, and keep polling while jobs are pending."
            ),
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
    # Return 403 in both "not found" and "not authorized" cases to prevent job-ID enumeration.
    if job is None or not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Job not found or not authorized.")
    output_mode = (
        request.query_params.get("mode")
        or request.headers.get("X-Aztea-Output-Mode")
        or "summary"
    )
    response = _job_response(job, caller, output_mode=str(output_mode or "summary"))
    response["latest_message_id"] = jobs.get_latest_message_id(job_id)
    quality = reputation.get_job_quality_rating(job_id)
    response["caller_quality_rating"] = quality.get("rating") if quality else None
    return JSONResponse(content=response)


@app.get(
    "/jobs/{job_id}/full",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_get_full_output(
    request: Request,
    job_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    job = jobs.get_job(job_id)
    # Return 403 in both "not found" and "not authorized" cases to prevent job-ID enumeration.
    if job is None or not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Job not found or not authorized.")
    return JSONResponse(
        content={
            "job_id": job_id,
            "status": job.get("status"),
            "output_payload": job.get("output_payload"),
        }
    )


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
        # Retroactively sign if the job is complete and the agent now has a key.
        # This covers two real cases: (1) the job completed before lazy-key
        # provisioning was wired up; (2) the lazy-provision blew up with an
        # exception that signed:None ate at completion time. Either way we can
        # produce a signature now without re-running the agent — the output
        # is already canonical and frozen on the row.
        agent_row = registry.get_agent(job.get("agent_id") or "", include_unapproved=True)
        if (
            agent_row is not None
            and str(job.get("status") or "").lower() == "complete"
            and job.get("output_payload") is not None
        ):
            try:
                from core import crypto as _crypto

                priv, _pub, did_v = registry.ensure_agent_signing_keys(
                    agent_row.get("agent_id") or ""
                )
                if priv and did_v:
                    sig_b64 = _crypto.sign_payload(priv, job.get("output_payload"))
                    sig_alg = str(agent_row.get("signing_alg") or "ed25519")
                    sig_at = datetime.now(timezone.utc).isoformat()
                    jobs.update_job_signature(
                        job["job_id"],
                        output_signature=sig_b64,
                        output_signature_alg=sig_alg,
                        output_signed_by_did=did_v,
                        output_signed_at=sig_at,
                    )
                    job = jobs.get_job(job_id) or job
                    signature = job.get("output_signature")
            except Exception:
                _LOG.exception("Retroactive signing failed for job %s", job_id)
    if not signature:
        status_lower = str(job.get("status") or "").lower()
        if status_lower != "complete":
            detail = (
                f"Job is in status '{status_lower or 'unknown'}'; "
                "signatures are emitted only on completed jobs."
            )
        else:
            detail = (
                "This completed job has no signature. The agent may have completed "
                "the job before signing keys were provisioned, or signing failed at "
                "completion time. The retroactive sign attempt also failed; contact "
                "support with this job_id."
            )
        raise HTTPException(status_code=404, detail=detail)
    agent_id = job.get("agent_id")
    base = (os.environ.get("SERVER_BASE_URL") or "").rstrip("/")
    verify_url = f"{base}/agents/{agent_id}/did.json" if base and agent_id else None
    output_payload = job.get("output_payload")
    output_hash = None
    try:
        from core import crypto as _crypto

        output_hash = hashlib.sha256(_crypto.canonical_json(output_payload)).hexdigest()
    except Exception:
        _LOG.exception("Failed to hash signed output for job %s", job_id)
    return JSONResponse(
        content={
            "job_id": job_id,
            "agent_id": agent_id,
            "agent_did": job.get("output_signed_by_did"),
            "did": job.get("output_signed_by_did"),
            "alg": job.get("output_signature_alg") or "ed25519",
            "signature": signature,
            "signed_at": job.get("output_signed_at"),
            "output_hash": output_hash,
            "verify_url": verify_url,
        }
    )


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
        raise HTTPException(
            status_code=403,
            detail="Agent listing is pending review and cannot accept jobs.",
        )

    if not _caller_worker_authorized_for_job(caller, job):
        status = 403 if caller["type"] == "agent_key" else 409
        detail = (
            "Not authorized for this agent job."
            if status == 403
            else "Job is not claimable."
        )
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
    claimed["caller_trust_score"] = _caller_trust_score(
        str(job.get("caller_owner_id") or "")
    )
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
        raise HTTPException(
            status_code=409, detail="Unable to heartbeat this job claim."
        )

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
            raise HTTPException(
                status_code=403, detail="Not authorized for this agent job."
            )
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
                output_format=(
                    str(body.output_format).strip().lower()
                    if body.output_format
                    else None
                ),
                protocol_metadata=_normalize_protocol_metadata(
                    body.protocol_metadata,
                    field_name="protocol_metadata",
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        agent = registry.get_agent(job["agent_id"], include_unapproved=True)
        if agent is None:
            raise HTTPException(
                status_code=404, detail=f"Agent '{job['agent_id']}' not found."
            )
        output_schema = agent.get("output_schema")
        if isinstance(output_schema, dict) and output_schema:
            mismatches = _validate_json_schema_subset(
                body.output_payload, output_schema
            )
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
                raise HTTPException(
                    status_code=409, detail="Unable to update job status."
                )
            settled_failed = _settle_failed_job(
                failed, actor_owner_id=actor_owner_id, event_type="job.failed_quality"
            )
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
            if not private_pem or not agent_did_value:
                # Same lazy-provision guarantee as the sync path so async
                # completions never silently drop signatures when the lifespan
                # backfill missed an agent.
                private_pem, _public_pem, agent_did_value = (
                    registry.ensure_agent_signing_keys(agent.get("agent_id") or "")
                )
            if (
                private_pem
                and agent_did_value
                and normalized_output_payload is not None
            ):
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
                "output_verification_deadline_at": updated.get(
                    "output_verification_deadline_at"
                ),
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
            judge_wallet = payments.get_or_create_wallet(
                f"agent:{quality['judge_agent_id']}"
            )
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
            _email.send_job_complete(
                caller_email, job_id, _agent_name, int(settled.get("price_cents") or 0)
            )
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
        # Return 403 in both "not found" and "not authorized" cases to prevent job-ID enumeration.
        if job is None or (
            caller["type"] != "master"
            and caller["owner_id"] != job.get("caller_owner_id")
        ):
            raise HTTPException(
                status_code=403, detail="Job not found or not authorized."
            )
        if job.get("status") != "complete" or not job.get("completed_at"):
            raise HTTPException(
                status_code=400,
                detail="Output verification is only available for completed jobs.",
            )
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
            deadline = _parse_iso_datetime(
                initialized.get("output_verification_deadline_at")
            )
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
                        payload={
                            "output_verification_deadline_at": expired.get(
                                "output_verification_deadline_at"
                            )
                        },
                    )

        if body.decision == "accept":
            if disputes.has_dispute_for_job(job_id):
                raise HTTPException(
                    status_code=409,
                    detail="Cannot accept output after a dispute is already filed.",
                )
            if verification_status == "accepted":
                settled = _settle_successful_job(
                    initialized,
                    actor_owner_id=caller["owner_id"],
                    require_dispute_window_expiry=False,
                )
                return _job_response(settled, caller), 200
            if verification_status in {"rejected", "expired"}:
                raise HTTPException(
                    status_code=409,
                    detail="Output verification decision is already closed for this job.",
                )
            decided = jobs.set_output_verification_decision(
                job_id,
                decision="accept",
                decision_owner_id=caller["owner_id"],
                reason=body.reason,
            )
            if decided is None:
                raise HTTPException(
                    status_code=409,
                    detail="Unable to record output verification decision.",
                )
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
            raise HTTPException(
                status_code=409,
                detail="Output verification decision is already closed for this job.",
            )

        rejection_reason = (
            body.reason or "Caller rejected output during verification window."
        )
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
