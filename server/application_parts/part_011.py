# server.application shard 11 — admin dispute review, job event hooks
# (CRUD + process + dead-letter), background sweep trigger, ops metrics +
# SLO, Stripe webhook + Connect onboarding, and spend / reconciliation
# helpers. This shard concentrates the operator-facing surface.


@app.get(
    "/admin/disputes/{dispute_id}",
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def admin_get_dispute(
    request: Request,
    dispute_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Full dispute context including job input/output and escrow balance."""
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    ctx = disputes.get_dispute_context(dispute_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
    escrow_wallet = payments.get_wallet_by_owner(
        payments.DISPUTE_ESCROW_OWNER_PREFIX + dispute_id
    )
    ctx["escrow_balance_cents"] = int((escrow_wallet or {}).get("balance_cents") or 0)
    return JSONResponse(content=ctx)


@app.post(
    "/admin/disputes/{dispute_id}/rule",
    response_model=core_models.DisputeJudgeResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("30/minute")
def disputes_admin_rule(
    request: Request,
    dispute_id: str,
    body: AdminDisputeRuleRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeJudgeResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    dispute_row = disputes.get_dispute(dispute_id)
    if dispute_row is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")

    if dispute_row["status"] in {"resolved", "consensus"}:
        disputes.set_dispute_status(dispute_id, "appealed")

    admin_user_id = None
    if caller["type"] == "user":
        admin_user_id = caller["user"]["user_id"]

    try:
        disputes.record_judgment(
            dispute_id,
            judge_kind="human_admin",
            verdict=body.outcome,
            reasoning=body.reasoning,
            admin_user_id=admin_user_id,
        )
        settlement = payments.post_dispute_settlement(
            dispute_id,
            outcome=body.outcome,
            split_caller_cents=body.split_caller_cents,
            split_agent_cents=body.split_agent_cents,
        )
        finalized = disputes.finalize_dispute(
            dispute_id,
            status="final",
            outcome=body.outcome,
            split_caller_cents=body.split_caller_cents,
            split_agent_cents=body.split_agent_cents,
        )
        if finalized is not None:
            _apply_dispute_effects(finalized, body.outcome)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except payments.InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": error_codes.DISPUTE_SETTLEMENT_INSUFFICIENT_BALANCE,
                "balance_cents": exc.balance_cents,
                "required_cents": exc.required_cents,
            },
        )

    if finalized is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")

    job = jobs.get_job(finalized["job_id"])
    if job is not None:
        _record_job_event(
            job,
            "job.dispute_finalized",
            actor_owner_id=caller["owner_id"],
            payload={"dispute_id": dispute_id, "outcome": body.outcome},
        )
        for _party_owner_id in {job.get("caller_owner_id"), job.get("agent_owner_id")}:
            _party_email = _get_owner_email(_party_owner_id or "")
            if _party_email:
                _email.send_dispute_resolved(_party_email, finalized["job_id"], dispute_id, body.outcome)
    return JSONResponse(content={"dispute": _dispute_view(finalized), "settlement": settlement})


@app.get(
    "/ops/jobs/{job_id}/settlement-trace",
    response_model=core_models.JobSettlementTraceResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_settlement_trace(
    request: Request,
    job_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobSettlementTraceResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    txs = payments.get_settlement_transactions(job["charge_tx_id"])
    distribution = payments.compute_success_distribution(
        int(job.get("price_cents") or 0),
        platform_fee_pct=job.get("platform_fee_pct_at_create"),
        fee_bearer_policy=job.get("fee_bearer_policy"),
    )
    fee_cents = int(distribution["platform_fee_cents"])
    return JSONResponse(
        content={
            "job_id": job["job_id"],
            "agent_id": job["agent_id"],
            "status": job["status"],
            "charge_tx_id": job["charge_tx_id"],
            "price_cents": job["price_cents"],
            "expected_agent_payout_cents": distribution["agent_payout_cents"],
            "expected_platform_fee_cents": fee_cents,
            "settled_at": job["settled_at"],
            "transactions": txs,
        }
    )


@app.get(
    "/ops/jobs/events",
    response_model=core_models.JobEventsResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def jobs_events(
    request: Request,
    since: int | None = None,
    limit: int = 100,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobEventsResponse:
    return JSONResponse(content={"events": _list_job_events(caller, since=since, limit=limit)})


@app.post(
    "/ops/jobs/hooks",
    status_code=201,
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 409, 422, 429, 500),
)
@limiter.limit("20/minute")
def job_event_hook_create(
    request: Request,
    body: JobEventHookCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _MAX_HOOKS_PER_OWNER = 20
    if caller["type"] != "master":
        existing = _list_job_event_hooks(owner_id=caller["owner_id"])
        if len(existing) >= _MAX_HOOKS_PER_OWNER:
            raise HTTPException(
                status_code=409,
                detail=error_codes.make_error(
                    error_codes.AUTH_HOOK_LIMIT,
                    f"You've reached the {_MAX_HOOKS_PER_OWNER} webhook limit. "
                    "Delete an existing hook to create a new one.",
                    {"max": _MAX_HOOKS_PER_OWNER, "current": len(existing)},
                ),
            )
    try:
        hook = _create_job_event_hook(caller["owner_id"], body.target_url, body.secret)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return JSONResponse(content=hook, status_code=201)


@app.get(
    "/ops/jobs/hooks",
    response_model=core_models.JobEventHookListResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def job_event_hook_list(
    request: Request,
    include_inactive: bool = False,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobEventHookListResponse:
    owner_id = None if caller["type"] == "master" else caller["owner_id"]
    hooks = _list_job_event_hooks(owner_id=owner_id, include_inactive=include_inactive)
    return JSONResponse(content={"hooks": hooks})


@app.delete(
    "/ops/jobs/hooks/{hook_id}",
    response_model=core_models.JobEventHookDeleteResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def job_event_hook_delete(
    request: Request,
    hook_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobEventHookDeleteResponse:
    owner_id = None if caller["type"] == "master" else caller["owner_id"]
    ok = _deactivate_job_event_hook(hook_id, owner_id=owner_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Hook not found.")
    return JSONResponse(content={"deleted": True, "hook_id": hook_id})


@app.post(
    "/ops/jobs/hooks/process",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def job_event_hook_process(
    request: Request,
    body: HookDeliveryProcessRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    summary = _process_due_hook_deliveries(limit=body.limit)
    return JSONResponse(content=summary)


@app.get(
    "/ops/jobs/hooks/dead-letter",
    response_model=core_models.JobEventHookDeadLetterResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def job_event_hook_dead_letter(
    request: Request,
    limit: int = 100,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobEventHookDeadLetterResponse:
    owner_id = None if _caller_has_scope(caller, "admin") else caller["owner_id"]
    deliveries = _list_hook_deliveries(owner_id=owner_id, status="failed", limit=limit)
    return JSONResponse(content={"deliveries": deliveries, "count": len(deliveries)})


@app.post(
    "/ops/jobs/sweep",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("20/minute")
def jobs_sweep(
    request: Request,
    body: JobsSweepRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    started = _utc_now_iso()
    try:
        summary = _sweep_jobs(
            retry_delay_seconds=body.retry_delay_seconds,
            sla_seconds=body.sla_seconds,
            limit=body.limit,
            actor_owner_id=caller["owner_id"],
        )
        _set_sweeper_state(last_run_at=started, last_summary=summary, last_error=None)
    except ValueError as exc:
        _set_sweeper_state(last_run_at=started, last_error=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))
    return JSONResponse(content=summary)


@app.get(
    "/ops/jobs/metrics",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def jobs_metrics(
    request: Request,
    sla_seconds: int = _DEFAULT_SLA_SECONDS,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    if sla_seconds <= 0:
        raise HTTPException(status_code=422, detail="sla_seconds must be > 0.")
    return JSONResponse(content=_jobs_metrics(sla_seconds=sla_seconds))


@app.get(
    "/ops/jobs/slo",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def jobs_slo(
    request: Request,
    sla_seconds: int = _DEFAULT_SLA_SECONDS,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    if sla_seconds <= 0:
        raise HTTPException(status_code=422, detail="sla_seconds must be > 0.")
    metrics = _jobs_metrics(sla_seconds=sla_seconds)
    return JSONResponse(content={"slo": metrics["slo"], "alerts": metrics["alerts"]})


# ---------------------------------------------------------------------------
# Payments ops routes
# ---------------------------------------------------------------------------

@app.get(
    "/ops/payments/reconcile",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def payments_reconcile_preview(
    request: Request,
    max_mismatches: int = 100,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    if max_mismatches <= 0:
        raise HTTPException(status_code=422, detail="max_mismatches must be > 0.")
    summary = payments.compute_ledger_invariants(max_mismatches=max_mismatches)
    return JSONResponse(content=summary)


@app.post(
    "/ops/payments/reconcile",
    status_code=201,
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def payments_reconcile_run(
    request: Request,
    body: ReconciliationRunRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    summary = payments.record_reconciliation_run(max_mismatches=body.max_mismatches)
    return JSONResponse(content=summary, status_code=201)


@app.get(
    "/ops/payments/reconcile/runs",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def payments_reconcile_runs(
    request: Request,
    limit: int = 20,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    if limit <= 0:
        raise HTTPException(status_code=422, detail="limit must be > 0.")
    runs = payments.list_reconciliation_runs(limit=limit)
    return JSONResponse(content={"runs": runs, "count": len(runs)})


# ---------------------------------------------------------------------------
# Spending summary
# ---------------------------------------------------------------------------


@app.get(
    "/wallets/spend-summary",
    responses=_error_responses(401, 403, 429, 500),
    tags=["Wallets"],
    summary="Rolling spend summary by period and per-agent breakdown.",
)
@limiter.limit("30/minute")
def wallet_spend_summary(
    request: Request,
    period: str = "7d",
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    period_map = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
    days = period_map.get(period, 7)
    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since_dt.isoformat()

    caller_owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(caller_owner_id)
    wallet_id = wallet["wallet_id"]

    with jobs._conn() as conn:
        rows = conn.execute(
            """
            SELECT agent_id, SUM(price_cents) AS total_cents, COUNT(*) AS job_count
            FROM jobs
            WHERE caller_owner_id = ?
              AND status IN ('complete', 'failed')
              AND created_at >= ?
            GROUP BY agent_id
            ORDER BY total_cents DESC
            LIMIT 100
            """,
            (caller_owner_id, since_iso),
        ).fetchall()
        totals = conn.execute(
            """
            SELECT SUM(price_cents) AS total_cents, COUNT(*) AS job_count
            FROM jobs
            WHERE caller_owner_id = ? AND created_at >= ?
            """,
            (caller_owner_id, since_iso),
        ).fetchone()

    by_agent = [
        {
            "agent_id": row["agent_id"],
            "total_cents": int(row["total_cents"] or 0),
            "job_count": int(row["job_count"] or 0),
        }
        for row in rows
    ]
    return JSONResponse(content={
        "period": period,
        "days": days,
        "total_cents": int((totals["total_cents"] or 0) if totals else 0),
        "total_jobs": int((totals["job_count"] or 0) if totals else 0),
        "by_agent": by_agent,
        "wallet_id": wallet_id,
    })


# ---------------------------------------------------------------------------
# Wallet routes
# ---------------------------------------------------------------------------

@app.post(
    "/wallets/deposit",
    response_model=core_models.WalletDepositResponse,
    responses=_error_responses(400, 401, 403, 404, 422, 429, 500),
)
@limiter.limit("20/minute")
def wallet_deposit(
    request: Request,
    body: DepositRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletDepositResponse:
    _require_scope(caller, "admin")
    _require_admin_ip_allowlist(request)
    wallet = payments.get_wallet(body.wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail=f"Wallet '{body.wallet_id}' not found.")
    if caller["type"] != "master" and wallet["owner_id"] != caller["owner_id"]:
        raise HTTPException(status_code=403, detail="Not authorized to deposit into this wallet.")
    if int(body.amount_cents) < MINIMUM_DEPOSIT_CENTS:
        raise _deposit_below_minimum_error(int(body.amount_cents))
    try:
        tx_id = payments.deposit(body.wallet_id, body.amount_cents, body.memo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    wallet = payments.get_wallet(body.wallet_id)
    return JSONResponse(content={
        "tx_id": tx_id, "wallet_id": body.wallet_id,
        "balance_cents": wallet["balance_cents"],
    })


@app.get(
    "/wallets/me",
    response_model=core_models.WalletResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def wallet_me(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletResponse:
    _require_any_scope(caller, "caller", "worker")
    owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(owner_id)
    txs = payments.get_wallet_transactions(wallet["wallet_id"], limit=50)
    caller_trust = payments.get_caller_trust(owner_id)
    return JSONResponse(content={**wallet, "caller_trust": caller_trust, "transactions": txs})


@app.post(
    "/wallets/me/daily-spend-limit",
    response_model=core_models.WalletDailySpendLimitResponse,
    responses=_error_responses(400, 401, 403, 429, 500),
    tags=["Wallets"],
    summary="Set or clear the authenticated wallet's rolling 24h spend cap.",
)
@limiter.limit("20/minute")
def wallet_set_daily_spend_limit(
    request: Request,
    body: core_models.WalletDailySpendLimitRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletDailySpendLimitResponse:
    _require_scope(caller, "caller")
    owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(owner_id)
    try:
        updated = payments.set_wallet_daily_spend_limit(
            wallet["wallet_id"],
            body.daily_spend_limit_cents,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(
        content={
            "wallet_id": updated["wallet_id"],
            "daily_spend_limit_cents": updated.get("daily_spend_limit_cents"),
        }
    )


@app.get(
    "/wallets/me/agent-earnings",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def wallet_me_agent_earnings(
    request: Request,
    _: core_models.CallerContext = Depends(_require_api_key),
):
    """Per-agent earnings breakdown for the authenticated user's wallet."""
    owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(owner_id)
    breakdown = payments.get_agent_earnings_breakdown(wallet["wallet_id"])
    # Enrich with agent names where available
    enriched = []
    for row in breakdown:
        agent_id = row["agent_id"]
        name = agent_id
        try:
            agent = registry.get_agent(agent_id, include_unapproved=True)
            if agent:
                name = agent.get("name") or agent_id
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            _LOG.warning("Failed to load agent name for earnings row %s: %s", agent_id, exc)
        enriched.append({**row, "agent_name": name})
    return JSONResponse(content={"earnings": enriched})

# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

@app.get(
    "/runs",
    response_model=core_models.RunsResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def get_runs(
    request: Request,
    limit: int = 50,
    _: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RunsResponse:
    limit = min(max(1, limit), 200)
    runs_file = os.path.join(_REPO_ROOT, "runs.jsonl")
    if not os.path.exists(runs_file):
        return JSONResponse(content={"runs": [], "skipped_lines": 0, "skipped_line_numbers": []})
    with open(runs_file, encoding="utf-8") as f:
        lines = f.readlines()
    runs = []
    skipped = 0
    skipped_line_numbers: list[int] = []
    for line_number, line in reversed(list(enumerate(lines, start=1))):
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            skipped += 1
            skipped_line_numbers.append(line_number)
            continue
        if len(runs) >= limit:
            break
    skipped_line_numbers.sort()
    return JSONResponse(
        content={
            "runs": runs,
            "skipped_lines": skipped,
            "skipped_line_numbers": skipped_line_numbers,
        },
        headers={"X-Skipped-Lines": str(skipped)},
    )


# ---------------------------------------------------------------------------
# Public config (Stripe publishable key for the frontend)
# ---------------------------------------------------------------------------

@app.get(
    "/config/public",
    tags=["config"],
    summary="Public server configuration for the frontend.",
)
def config_public() -> JSONResponse:
    return JSONResponse({
        "stripe_enabled": bool(_STRIPE_SECRET_KEY and _STRIPE_AVAILABLE),
        "stripe_publishable_key": _STRIPE_PUBLISHABLE_KEY or None,
    })


@app.get(
    "/public/docs",
    tags=["docs"],
    summary="List platform documentation available from this deployment.",
)
def public_docs_index() -> JSONResponse:
    entries = _public_docs_entries()
    docs = [
        {
            "slug": item["slug"],
            "title": item["title"],
            "path": f"/public/docs/{item['slug']}",
        }
        for item in entries
    ]
    return JSONResponse({"docs": docs, "count": len(docs)})


@app.get(
    "/public/docs/{doc_slug}",
    tags=["docs"],
    summary="Fetch a public documentation file by slug.",
)
def public_doc_content(doc_slug: str) -> JSONResponse:
    doc = _find_public_doc(doc_slug)
    if doc is None:
        raise HTTPException(status_code=404, detail="Documentation page not found.")
    try:
        with open(doc["full_path"], encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        raise HTTPException(status_code=500, detail="Unable to read documentation file.") from None
    return JSONResponse({
        "slug": doc["slug"],
        "title": doc["title"],
        "content": content,
    })


# ---------------------------------------------------------------------------
# Stripe: create checkout session + webhook
# ---------------------------------------------------------------------------


def _extract_stripe_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code).strip().lower()
    nested = getattr(exc, "error", None)
    nested_code = getattr(nested, "code", None) if nested is not None else None
    if nested_code:
        return str(nested_code).strip().lower()
    return ""


def _stripe_http_error(operation: str, exc: Exception) -> tuple[int, dict[str, Any]]:
    code = _extract_stripe_error_code(exc)
    message = str(exc or "").strip().lower()
    if code in {"insufficient_funds", "balance_insufficient"} or "insufficient" in message:
        return 400, {
            "error": "payment.stripe_insufficient_funds",
            "message": "Payouts are temporarily unavailable because Stripe platform balance is insufficient.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if code in {"account_closed", "account_invalid", "no_such_destination"} or "no such destination" in message:
        return 400, {
            "error": "payment.stripe_destination_invalid",
            "message": "Your connected payout account is unavailable. Reconnect your bank account and try again.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if "signed up for connect" in message or "connect is not enabled" in message:
        return 503, {
            "error": "payment.stripe_connect_unavailable",
            "message": "Stripe Connect is not enabled for this server account.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if code in {"rate_limit", "rate_limit_error"}:
        return 429, {
            "error": "payment.stripe_rate_limited",
            "message": "Stripe is rate-limiting requests right now. Please retry shortly.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if code in {"authentication_error", "permission_error"}:
        return 503, {
            "error": "payment.stripe_auth_error",
            "message": "Payment processing is temporarily unavailable due to Stripe configuration.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if code in {"api_connection_error", "api_error"}:
        return 502, {
            "error": "payment.stripe_upstream_error",
            "message": "Stripe is temporarily unavailable. Please try again.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    return 502, {
        "error": "payment.stripe_error",
        "message": "Stripe request failed. Please try again.",
        "data": {"stripe_code": code or None, "operation": operation},
    }


def _stripe_obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stripe_obj_id(obj: Any) -> str:
    value = _stripe_obj_get(obj, "id", "") or ""
    return str(value).strip()


def _stripe_begin_checkout_webhook_event(
    *,
    session_id: str,
    wallet_id: str,
    amount_cents: int,
) -> str:
    now = _utc_now_iso()
    with get_db_connection() as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        processed_row = conn.execute(
            "SELECT 1 FROM stripe_sessions WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        if processed_row is not None:
            conn.commit()
            return "already_processed"
        state_row = conn.execute(
            """
            SELECT status
            FROM stripe_webhook_events
            WHERE session_id = ?
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if state_row is None:
            conn.execute(
                """
                INSERT INTO stripe_webhook_events
                    (session_id, wallet_id, amount_cents, status, attempts, created_at, updated_at)
                VALUES (?, ?, ?, 'processing', 1, ?, ?)
                """,
                (session_id, wallet_id, int(amount_cents), now, now),
            )
            conn.commit()
            return "acquired"
        status = str(state_row[0] or "").strip().lower()
        if status == "processed":
            conn.commit()
            return "already_processed"
        if status == "processing":
            conn.commit()
            return "already_processing"
        conn.execute(
            """
            UPDATE stripe_webhook_events
            SET wallet_id = ?,
                amount_cents = ?,
                status = 'processing',
                attempts = attempts + 1,
                last_error = NULL,
                updated_at = ?
            WHERE session_id = ?
            """,
            (wallet_id, int(amount_cents), now, session_id),
        )
        conn.commit()
    return "acquired"


def _stripe_mark_checkout_webhook_failed(
    *,
    session_id: str,
    error_message: str,
) -> None:
    with get_db_connection() as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            UPDATE stripe_webhook_events
            SET status = 'failed',
                last_error = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (str(error_message or "")[:1000], _utc_now_iso(), session_id),
        )
        conn.commit()


def _stripe_mark_checkout_webhook_processed(
    *,
    session_id: str,
    wallet_id: str,
    amount_cents: int,
) -> None:
    now = _utc_now_iso()
    with get_db_connection() as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT OR IGNORE INTO stripe_sessions (session_id, wallet_id, amount_cents, processed_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, wallet_id, int(amount_cents), now),
        )
        conn.execute(
            """
            UPDATE stripe_webhook_events
            SET status = 'processed',
                last_error = NULL,
                updated_at = ?
            WHERE session_id = ?
            """,
            (now, session_id),
        )
        conn.commit()


def _wallet_stripe_topup_total_last_24h(wallet_id: str) -> int:
    window_start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM stripe_sessions
            WHERE wallet_id = ? AND processed_at >= ?
            """,
            (wallet_id, window_start),
        ).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


