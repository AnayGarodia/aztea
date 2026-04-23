# server.application shard 8 — registry search + admin review + registry
# sync call flow (pre-charge → dispatch → settle/refund, with built-in
# agent routing, verifier hooks, and dispute-window enforcement).
# Variable-pricing helpers (_estimate_variable_charge,
# _resolve_agent_pricing, _maybe_refund_pricing_diff) live in part_004.


@app.post(
    "/registry/search",
    response_model=core_models.RegistrySearchResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
)
@limiter.limit(_SEARCH_RATE_LIMIT)
def registry_search(
    request: Request,
    body: RegistrySearchRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistrySearchResponse:
    """
    Recommended discovery endpoint.
    Performs semantic natural-language matching with trust, pricing, and input-schema compatibility checks.
    The legacy GET /registry/agents?tag=... route remains supported for backward compatibility.
    """
    try:
        include_unapproved = _caller_is_admin(caller)
        caller_trust = None
        if body.respect_caller_trust_min and caller["type"] != "master":
            caller_trust = _caller_trust_score(caller["owner_id"])
        ranked = registry.search_agents(
            query=body.query,
            limit=body.limit,
            min_trust=body.min_trust,
            max_price_cents=body.max_price_cents,
            required_input_fields=body.required_input_fields,
            caller_trust=caller_trust,
            include_unapproved=include_unapproved,
            model_provider=body.model_provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    search_stats = _compute_bulk_agent_stats([item["agent"]["agent_id"] for item in ranked])
    results = [
        {
            "agent": _agent_response(item["agent"], caller, search_stats.get(item["agent"]["agent_id"])),
            "similarity": item["similarity"],
            "trust": item["trust"],
            "blended_score": item["blended_score"],
            "match_reasons": item["match_reasons"],
        }
        for item in ranked
    ]
    return JSONResponse(content={"results": results, "count": len(results)})


@app.get(
    "/registry/agents/{agent_id}",
    response_model=core_models.AgentResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def registry_get(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentResponse:
    _require_any_scope(caller, "caller", "worker")
    include_unapproved = _caller_is_admin(caller)
    agent = registry.get_agent_with_reputation(agent_id, include_unapproved=include_unapproved)
    if agent is None or agent.get("status") == "banned" or not _caller_can_access_agent(caller, agent):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    agent_stats = _compute_bulk_agent_stats([agent_id]).get(agent_id)
    return JSONResponse(content=_agent_response(agent, caller, agent_stats))


@app.get(
    "/registry/agents/{agent_id}/work-history",
    response_model=core_models.DynamicListResponse,
    responses=_error_responses(401, 404, 429, 500),
)
@limiter.limit("60/minute")
def registry_agent_work_history(
    request: Request,
    agent_id: str,
    limit: int = 20,
    offset: int = 0,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicListResponse:
    """Return paginated public work examples for an agent."""
    capped_limit = max(1, min(int(limit), 50))
    capped_offset = max(0, int(offset))
    agent = registry.get_agent(agent_id)
    if agent is None or agent.get("status") == "banned" or not _caller_can_access_agent(caller, agent):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    examples: list = agent.get("output_examples") or []
    page = examples[capped_offset: capped_offset + capped_limit]
    return JSONResponse(content={"items": page, "total": len(examples), "limit": capped_limit, "offset": capped_offset})


@app.get(
    "/llm/providers",
    responses=_error_responses(401, 429, 500),
)
@limiter.limit("60/minute")
def llm_providers_list(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
):
    """List all registered LLM providers and their availability."""
    from core.llm import registry as llm_registry
    providers = llm_registry.list_providers()
    return JSONResponse(content={"providers": providers})


@app.get(
    "/registry/agents/{agent_id}/keys",
    response_model=core_models.AgentKeyListResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def registry_agent_key_list(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentKeyListResponse:
    _require_scope(caller, "worker")
    if caller["type"] == "agent_key":
        raise HTTPException(status_code=403, detail="Agent-scoped keys cannot list keys.")
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    keys = _auth.list_agent_api_keys(agent_id)
    return JSONResponse(content={"keys": keys})


@app.post(
    "/registry/agents/{agent_id}/keys",
    status_code=201,
    response_model=core_models.AgentKeyCreateResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def registry_agent_key_create(
    request: Request,
    agent_id: str,
    body: AgentKeyCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentKeyCreateResponse:
    _require_scope(caller, "worker")
    if caller["type"] == "agent_key":
        raise HTTPException(status_code=403, detail="Agent-scoped keys cannot mint new keys.")
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    key = _auth.create_agent_api_key(agent_id, name=body.name)
    return JSONResponse(
        content={
            "key_id": key["key_id"],
            "agent_id": key["agent_id"],
            "raw_key": key["raw_key"],
            "key_prefix": key["key_prefix"],
            "created_at": key["created_at"],
        },
        status_code=201,
    )


@app.post(
    "/admin/agents/{agent_id}/suspend",
    response_model=core_models.AgentResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def admin_agent_suspend(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    agent = registry.set_agent_status(agent_id, "suspended")
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return JSONResponse(content=_agent_response(agent, caller))


@app.post(
    "/admin/agents/{agent_id}/ban",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def admin_agent_ban(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    agent = registry.set_agent_status(agent_id, "banned")
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    summary = _fail_open_jobs_for_agent(
        agent_id,
        actor_owner_id=caller["owner_id"],
        reason="Agent was banned by an administrator.",
    )
    return JSONResponse(content={"agent": _agent_response(agent, caller), "ban_summary": summary})


@app.get(
    "/admin/agents/review-queue",
    response_model=core_models.RegistryAgentsResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def admin_agents_review_queue(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistryAgentsResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    agents = registry.list_pending_review_agents()
    return JSONResponse(content={"agents": [_agent_response(agent, caller) for agent in agents], "count": len(agents)})


@app.post(
    "/admin/agents/{agent_id}/review",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("30/minute")
def admin_review_agent(
    request: Request,
    agent_id: str,
    body: AgentReviewDecisionRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    reviewed = registry.set_agent_review_decision(
        agent_id,
        decision=body.decision,
        note=body.note,
        reviewed_by=caller["owner_id"],
        reviewed_at=_utc_now_iso(),
    )
    if reviewed is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    health_probe: dict[str, Any] | None = None
    probe_url = str(reviewed.get("healthcheck_url") or "").strip()
    if body.decision == "approve" and probe_url:
        try:
            ok, error_text = _probe_agent_endpoint_health(
                probe_url,
                timeout_seconds=_ENDPOINT_MONITOR_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            ok = False
            error_text = str(exc)
        endpoint_status = "healthy" if ok else "degraded"
        endpoint_failures = 0 if ok else max(1, int(reviewed.get("endpoint_consecutive_failures") or 0) + 1)
        reviewed = registry.set_agent_endpoint_health(
            agent_id,
            endpoint_health_status=endpoint_status,
            endpoint_consecutive_failures=endpoint_failures,
            endpoint_last_checked_at=_utc_now_iso(),
            endpoint_last_error=None if ok else error_text,
        ) or reviewed
        health_probe = {"ok": bool(ok), "error": error_text}

    return JSONResponse(content={"agent": _agent_response(reviewed, caller), "health_probe": health_probe})


@app.post(
    "/registry/agents/{agent_id}/call",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 429, 500, 502, 503),
)
@limiter.limit("10/minute")
def registry_call(
    request: Request,
    agent_id: str,
    body: core_models.RegistryCallRequest | None = Body(default=None),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> Response:
    """
    Invoke a registered agent with full payment lifecycle:
      1. Deduct price (402 if broke).
      2. Dispatch call (internal handler for internal:// endpoints, HTTP otherwise).
      3a. Success → payout 90% agent / 10% platform.
      3b. Failure → full refund to caller.
    """
    _require_scope(caller, "caller")
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_access_agent(caller, agent):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    _assert_agent_callable(agent_id, agent)
    builtin_agent_id = _resolve_builtin_agent_id(agent)
    safe_endpoint_url = ""
    if builtin_agent_id is None:
        try:
            safe_endpoint_url = _validate_agent_endpoint_url(request, str(agent.get("endpoint_url") or ""))
        except ValueError as exc:
            _LOG.warning("Blocked misconfigured endpoint for agent %s: %s", agent_id, exc)
            raise HTTPException(status_code=502, detail="Agent endpoint is misconfigured.")

    caller_owner_id = _caller_owner_id(request)
    fee_bearer_policy = "caller"
    platform_fee_pct_at_create = int(payments.PLATFORM_FEE_PCT)
    payload = dict(body.root) if body is not None else {}
    requested_output_formats: list[str] = []
    try:
        payload, requested_output_formats = _normalize_input_protocol_from_payload(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                str(exc),
                {"agent_id": agent_id},
            ),
        )
    pricing_estimate = _estimate_variable_charge(
        agent=agent,
        payload=payload,
        per_job_cap_cents=_caller_key_per_job_cap(caller),
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
                    "detail": pricing_estimate.get("detail"),
                },
            ),
        )
    price_cents = int(pricing_estimate["price_cents"])
    success_distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy,
    )
    caller_charge_cents = int(success_distribution["caller_charge_cents"])
    caller_wallet   = payments.get_or_create_wallet(caller_owner_id)
    # Payouts settle to the canonical agent wallet keyed by agent_id.
    _agent_payout_owner = f"agent:{agent['agent_id']}"
    agent_wallet    = payments.get_or_create_wallet(_agent_payout_owner)
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    charge_tx_id = _pre_call_charge_or_402(
        caller=caller,
        caller_wallet_id=caller_wallet["wallet_id"],
        charge_cents=caller_charge_cents,
        agent_id=agent_id,
    )
    start = time.monotonic()
    if builtin_agent_id is not None:
        try:
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
                input_payload=payload,
                agent_owner_id=agent.get("owner_id"),
                max_attempts=1,
                dispute_window_hours=_DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
                judge_agent_id=_extract_judge_agent_id(agent.get("input_schema")) or _QUALITY_JUDGE_AGENT_ID,
            )
        except Exception:
            payments.post_call_refund(
                caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent["agent_id"]
            )
            _LOG.exception("Failed to create sync job for built-in agent %s.", agent["agent_id"])
            raise HTTPException(status_code=500, detail="Failed to create job.")
        _record_job_event(
            job,
            "job.created",
            actor_owner_id=caller["owner_id"],
            payload={"source": "registry_call_sync", "max_attempts": 1},
        )
        try:
            output = _execute_builtin_agent(builtin_agent_id, payload)
            output = _normalize_output_protocol_for_response(
                output,
                requested_output_formats=requested_output_formats,
            )
            completed = jobs.update_job_status(
                job["job_id"],
                "complete",
                output_payload=output,
                completed=True,
            )
            if completed is None:
                raise RuntimeError("Failed to mark built-in sync job complete.")
            _record_job_event(
                completed,
                "job.completed",
                actor_owner_id=caller["owner_id"],
                payload={"status": completed["status"], "source": "registry_call_sync"},
            )
            _settle_successful_job(completed, actor_owner_id=caller["owner_id"])
            _maybe_refund_pricing_diff(
                agent=agent,
                payload=payload,
                output=output,
                caller_wallet_id=caller_wallet["wallet_id"],
                agent_wallet_id=agent_wallet["wallet_id"],
                platform_wallet_id=platform_wallet["wallet_id"],
                charge_tx_id=charge_tx_id,
                estimate=pricing_estimate,
                caller_charge_cents=caller_charge_cents,
                success_distribution=success_distribution,
                platform_fee_pct=platform_fee_pct_at_create,
                fee_bearer_policy=fee_bearer_policy,
            )
            _record_public_work_example(
                agent,
                payload,
                output,
                job_id=job["job_id"],
                latency_ms=_job_latency_ms(completed),
            )
            return JSONResponse(content=output)
        except ValidationError as exc:
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message="Request validation failed.",
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_validation",
                )
            def _sanitize_errors(errors):
                clean = []
                for e in errors:
                    entry = {k: v for k, v in e.items() if k != "ctx"}
                    ctx = e.get("ctx")
                    if ctx:
                        entry["ctx"] = {k: str(v) for k, v in ctx.items()}
                    clean.append(entry)
                return clean

            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    "Request validation failed.",
                    {"errors": _sanitize_errors(exc.errors())},
                ),
            )
        except ValueError as exc:
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message=str(exc),
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_input",
                )
            message = str(exc)
            status = 422 if message.startswith("Invalid ticker symbol:") else 400
            raise HTTPException(status_code=status, detail=message)
        except _groq.RateLimitError as exc:
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message=f"All LLM models rate-limited. ({exc})",
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_rate_limit",
                )
            raise HTTPException(status_code=503, detail=f"All LLM models rate-limited. ({exc})")
        except Exception:
            _LOG.exception("Built-in agent execution failed for %s.", builtin_agent_id)
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message="Agent execution failed.",
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_builtin",
                )
            raise HTTPException(status_code=500, detail="Agent execution failed.")

    # --- Input payload size cap (256 KB) ---
    try:
        payload_bytes = len(json.dumps(payload).encode("utf-8"))
    except Exception:
        payload_bytes = 0
    if payload_bytes > 256 * 1024:
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent_id
        )
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                error_codes.PAYLOAD_TOO_LARGE,
                "Input payload exceeds the 256 KB limit. Agents cannot process payloads this large — "
                "try summarizing or splitting into multiple calls.",
                {"size_bytes": payload_bytes, "limit_bytes": 256 * 1024},
            ),
        )

    # --- Input schema validation (if agent declared one) ---
    agent_input_schema = agent.get("input_schema")
    if isinstance(agent_input_schema, dict) and agent_input_schema:
        try:
            import jsonschema as _jsc
            _jsc.validate(instance=payload, schema=agent_input_schema)
        except ImportError:
            pass
        except Exception as _schema_exc:
            payments.post_call_refund(
                caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent_id
            )
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.INPUT_SCHEMA_VIOLATION,
                    f"Input validation failed: {_schema_exc.message if hasattr(_schema_exc, 'message') else str(_schema_exc)}",
                    {"path": list(getattr(_schema_exc, "absolute_path", []))},
                ),
            )

    try:
        proxy_agent = dict(agent)
        proxy_agent["endpoint_url"] = safe_endpoint_url
        resp = http.post(
            safe_endpoint_url,
            json=payload,
            headers=_proxy_headers_for_agent(proxy_agent),
            timeout=120,
            allow_redirects=False,
        )
    except http.exceptions.Timeout:
        latency_ms = (time.monotonic() - start) * 1000
        registry.update_call_stats(agent_id, latency_ms, False)
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent_id
        )
        _LOG.warning("Agent call timed out for %s", agent_id)
        raise HTTPException(
            status_code=504,
            detail=error_codes.make_error(
                error_codes.AGENT_CALL_TIMEOUT,
                "Agent didn't respond within 120 seconds. You were not charged.",
                {"agent_id": agent_id},
            ),
        )
    except http.RequestException as e:
        latency_ms = (time.monotonic() - start) * 1000
        registry.update_call_stats(agent_id, latency_ms, False)
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent_id
        )
        _LOG.warning("Upstream agent unreachable for %s: %s", agent_id, type(e).__name__)
        raise HTTPException(
            status_code=502,
            detail=error_codes.make_error(
                error_codes.AGENT_ENDPOINT_OFFLINE,
                "This agent's endpoint is offline or unreachable. You were not charged.",
                {"agent_id": agent_id},
            ),
        )

    latency_ms = (time.monotonic() - start) * 1000
    status_code = int(resp.status_code)
    success = 200 <= status_code < 300
    registry.update_call_stats(agent_id, latency_ms, success)

    if not success:
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent_id
        )
        if 400 <= status_code < 500:
            # Surface agent's own error message (truncated) but never expose internals
            try:
                agent_err = resp.json()
                agent_msg = str(agent_err.get("error") or agent_err.get("message") or agent_err.get("detail") or "")[:500]
            except Exception:
                agent_msg = ""
            msg = "Agent rejected the request. You were not charged."
            if agent_msg:
                msg = f"Agent rejected the request: {agent_msg}. You were not charged."
            raise HTTPException(
                status_code=status_code,
                detail=error_codes.make_error(
                    error_codes.AGENT_REJECTED_REQUEST,
                    msg,
                    {"agent_id": agent_id, "agent_status": status_code},
                ),
            )
        raise HTTPException(
            status_code=502,
            detail=error_codes.make_error(
                error_codes.AGENT_INTERNAL_ERROR,
                "Agent is experiencing errors. You were not charged.",
                {"agent_id": agent_id, "agent_status": status_code},
            ),
        )

    # --- Output size cap (1 MB) ---
    raw_content = resp.content
    if len(raw_content) > 1_048_576:
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent_id
        )
        raise HTTPException(
            status_code=502,
            detail=error_codes.make_error(
                error_codes.AGENT_RESPONSE_TOO_LARGE,
                "Agent returned a response larger than 1 MB. You were not charged. Contact the agent owner.",
                {"agent_id": agent_id, "size_bytes": len(raw_content)},
            ),
        )

    # --- Non-JSON response handling ---
    content_type = resp.headers.get("content-type", "").lower()
    if "application/json" not in content_type and "text/json" not in content_type:
        try:
            json.loads(raw_content)
        except Exception:
            payments.post_call_refund(
                caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent_id
            )
            raise HTTPException(
                status_code=502,
                detail=error_codes.make_error(
                    error_codes.AGENT_INVALID_RESPONSE,
                    "Agent returned a malformed response (not valid JSON). You were not charged.",
                    {"agent_id": agent_id},
                ),
            )

    payments.post_call_payout(
        agent_wallet["wallet_id"], platform_wallet["wallet_id"],
        charge_tx_id, price_cents, agent_id,
        platform_fee_pct=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy,
    )

    return _proxy_response(resp)


# ---------------------------------------------------------------------------
# Jobs routes
# ---------------------------------------------------------------------------

@app.post(
    "/jobs",
    status_code=201,
    response_model=core_models.JobResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 422, 429, 500, 503),
)
@limiter.limit(_JOBS_CREATE_RATE_LIMIT)
def jobs_create(
    request: Request,
    body: JobCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "caller")
    parent_job = _resolve_parent_job_for_creation(
        caller,
        body.parent_job_id,
        parent_cascade_policy=body.parent_cascade_policy,
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
    agent = registry.get_agent(body.agent_id, include_unapproved=True)
    if agent is None or not _caller_can_access_agent(caller, agent):
        raise HTTPException(status_code=404, detail=f"Agent '{body.agent_id}' not found.")
    _assert_agent_callable(body.agent_id, agent)

    # Validate callback_url at creation time (not just at delivery)
    if body.callback_url:
        try:
            _validate_hook_url(str(body.callback_url))
        except (ValueError, HTTPException) as exc:
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    f"callback_url is invalid: {exc}",
                    {"field": "callback_url"},
                ),
            )

    caller_owner_id = _caller_owner_id(request)
    min_caller_trust = _extract_caller_trust_min(agent.get("input_schema"))
    if min_caller_trust is not None and caller["type"] != "master":
        caller_trust = _caller_trust_score(caller_owner_id)
        if caller_trust < min_caller_trust:
            raise HTTPException(
                status_code=403,
                detail=error_codes.make_error(
                    error_codes.UNAUTHORIZED,
                    "Caller trust is below this agent's required minimum.",
                    {
                        "caller_trust": round(caller_trust, 6),
                        "required_min_caller_trust": round(min_caller_trust, 6),
                        "agent_id": agent["agent_id"],
                    },
                ),
            )

    pricing_estimate = _estimate_variable_charge(
        agent=agent,
        payload=body.input_payload,
        budget_cents=body.budget_cents,
        per_job_cap_cents=_caller_key_per_job_cap(caller),
    )
    if pricing_estimate.get("cap_violated"):
        violation = pricing_estimate["cap_violated"]
        if violation["scope"] == "budget":
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.BUDGET_EXCEEDED,
                    (
                        f"Variable-price estimate "
                        f"({violation['price_cents']}¢) exceeds your budget "
                        f"({violation['limit_cents']}¢)."
                    ),
                    {
                        "price_cents": violation["price_cents"],
                        "budget_cents": violation["limit_cents"],
                        "pricing_model": pricing_estimate["pricing_model"],
                        "detail": pricing_estimate.get("detail"),
                        "agent_id": agent["agent_id"],
                    },
                ),
            )
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
                    "detail": pricing_estimate.get("detail"),
                },
            ),
        )
    price_cents = int(pricing_estimate["price_cents"])
    fee_bearer_policy = payments.normalize_fee_bearer_policy(body.fee_bearer_policy)
    platform_fee_pct_at_create = int(payments.PLATFORM_FEE_PCT)
    success_distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy,
    )
    caller_charge_cents = int(success_distribution["caller_charge_cents"])
    if caller_charge_cents <= 0 and price_cents > 0:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_CHARGE_AMOUNT,
                "Computed caller charge is non-positive.",
                {"caller_charge_cents": caller_charge_cents, "price_cents": price_cents},
            ),
        )
    if caller_charge_cents > price_cents * 2:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.CHARGE_EXCEEDS_LISTED_PRICE,
                "Caller charge must not exceed twice the listed price.",
                {"caller_charge_cents": caller_charge_cents, "price_cents": price_cents},
            ),
        )
    if body.budget_cents is not None and price_cents > body.budget_cents:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.BUDGET_EXCEEDED,
                f"Agent price ({price_cents}¢) exceeds your budget ({body.budget_cents}¢).",
                {"price_cents": price_cents, "budget_cents": body.budget_cents, "agent_id": agent["agent_id"]},
            ),
        )
    if price_cents > 2000 and not _agent_has_verified_contract(agent):
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.VERIFIED_CONTRACT_REQUIRED,
                "Jobs above $20 require a worker with a verified input/output contract.",
                {"agent_id": agent["agent_id"], "price_cents": price_cents},
            ),
        )
    key_per_job_cap_cents = _caller_key_per_job_cap(caller)
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
                },
            ),
        )
    output_verification_window_seconds = (
        86400
        if body.output_verification_window_seconds is None
        else body.output_verification_window_seconds
    )
    try:
        input_payload = _merge_protocol_input_envelope(
            body.input_payload,
            input_artifacts=_normalize_protocol_artifact_list(
                body.input_artifacts,
                field_name="input_artifacts",
            ),
            preferred_input_formats=_normalize_format_preferences(
                body.preferred_input_formats,
                field_name="preferred_input_formats",
            ),
            preferred_output_formats=_normalize_format_preferences(
                body.preferred_output_formats,
                field_name="preferred_output_formats",
            ),
            communication_channel=_normalize_protocol_channel(
                body.communication_channel,
                field_name="communication_channel",
            ),
            protocol_metadata=_normalize_protocol_metadata(
                body.protocol_metadata,
                field_name="protocol_metadata",
            ),
            private_task=bool(body.private_task),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    _agent_payout_owner2 = f"agent:{agent['agent_id']}"
    agent_wallet = payments.get_or_create_wallet(_agent_payout_owner2)
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    charge_tx_id = _pre_call_charge_or_402(
        caller=caller,
        caller_wallet_id=caller_wallet["wallet_id"],
        charge_cents=caller_charge_cents,
        agent_id=agent["agent_id"],
    )

    try:
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
            max_attempts=body.max_attempts,
            parent_job_id=(parent_job or {}).get("job_id"),
            tree_depth=tree_depth,
            parent_cascade_policy=body.parent_cascade_policy,
            clarification_timeout_seconds=body.clarification_timeout_seconds,
            clarification_timeout_policy=body.clarification_timeout_policy,
            dispute_window_hours=body.dispute_window_hours or _DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
            judge_agent_id=_extract_judge_agent_id(agent.get("input_schema")) or _QUALITY_JUDGE_AGENT_ID,
            callback_url=body.callback_url or None,
            callback_secret=body.callback_secret or None,
            output_verification_window_seconds=output_verification_window_seconds,
        )
    except Exception:
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent["agent_id"]
        )
        _LOG.exception("Failed to create job for agent %s.", agent["agent_id"])
        raise HTTPException(status_code=500, detail="Failed to create job.")

    _record_job_event(
        job,
        "job.created",
        actor_owner_id=caller["owner_id"],
        payload={
            "max_attempts": body.max_attempts,
            "parent_job_id": (parent_job or {}).get("job_id"),
            "parent_cascade_policy": body.parent_cascade_policy,
            "tree_depth": tree_depth,
        },
    )
    return JSONResponse(content=_job_response(job, caller), status_code=201)


