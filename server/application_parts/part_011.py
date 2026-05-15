
from core import db as _db
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
        raise HTTPException(
            status_code=404, detail=f"Dispute '{dispute_id}' not found."
        )
    escrow_wallet = payments.get_wallet_by_owner(
        payments.DISPUTE_ESCROW_OWNER_PREFIX + dispute_id
    )
    ctx["escrow_balance_cents"] = int((escrow_wallet or {}).get("balance_cents") or 0)
    return JSONResponse(content=ctx)


@app.post(
    "/admin/disputes/{dispute_id}/rule",
    response_model=core_models.DisputeJudgeResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
    dependencies=[Depends(_require_admin_caller)],
)
@limiter.limit("30/minute")
def disputes_admin_rule(
    request: Request,
    dispute_id: str,
    body: AdminDisputeRuleRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeJudgeResponse:
    # Belt-and-braces: route-level dep above already 403s non-admin callers
    # before body parse, so this in-handler check is redundant for fresh
    # requests. Kept so future refactors that move the route can't silently
    # drop the scope guard.
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    dispute_row = disputes.get_dispute(dispute_id)
    if dispute_row is None:
        raise HTTPException(
            status_code=404, detail=f"Dispute '{dispute_id}' not found."
        )

    if dispute_row["status"] == "final":
        raise HTTPException(
            status_code=409,
            detail="This dispute is already finalized and cannot be re-ruled.",
        )

    if dispute_row["status"] in {"resolved", "consensus"}:
        disputes.set_dispute_status(dispute_id, "appealed")
        disputes.append_audit_event(
            dispute_id,
            event="status_change",
            actor=caller["owner_id"],
            extra={"from": dispute_row["status"], "to": "appealed"},
        )

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
        disputes.append_audit_event(
            dispute_id,
            event="admin_rule",
            actor=str(admin_user_id or caller["owner_id"]),
            extra={"outcome": body.outcome, "reasoning": body.reasoning[:200]},
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
        raise HTTPException(
            status_code=404, detail=f"Dispute '{dispute_id}' not found."
        )

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
                _email.send_dispute_resolved(
                    _party_email, finalized["job_id"], dispute_id, body.outcome
                )
    return JSONResponse(
        content={"dispute": _dispute_view(finalized), "settlement": settlement}
    )


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
    return JSONResponse(
        content={"events": _list_job_events(caller, since=since, limit=limit)}
    )


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
    except _db.IntegrityError as exc:
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
    body: ReconciliationRunRequest | None = Body(default=None),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    # Body is optional: an empty POST defaults to a no-op-shaped request so
    # ops smoke checks (`curl -X POST .../reconcile`) don't trip pydantic
    # "Field required" errors before the admin gate even runs.
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    effective = body or ReconciliationRunRequest()
    summary = payments.record_reconciliation_run(max_mismatches=effective.max_mismatches)
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
    include_sunset: bool = False,
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
            WHERE caller_owner_id = %s
              AND status IN ('complete', 'failed', 'stopped')
              AND created_at >= %s
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
            WHERE caller_owner_id = %s AND created_at >= %s
            """,
            (caller_owner_id, since_iso),
        ).fetchone()

    by_agent = []
    sunset_by_agent = []
    sunset_ids = set(_builtin_constants.SUNSET_DEPRECATED_AGENT_IDS)
    for row in rows:
        agent_id = row["agent_id"]
        agent_name = agent_id
        agent_review_status = ""
        if agent_id:
            try:
                ag = registry.get_agent(agent_id, include_unapproved=True)
                if ag:
                    agent_name = ag.get("name") or agent_id
                    agent_review_status = (
                        str(ag.get("review_status") or "").strip().lower()
                    )
            except Exception:
                _LOG.warning("Failed to resolve agent name for %s in spending report", agent_id, exc_info=True)
        is_sunset = (
            agent_id in sunset_ids or agent_review_status == "sunset"
        )
        item = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "total_cents": int(row["total_cents"] or 0),
            "job_count": int(row["job_count"] or 0),
            "is_sunset": is_sunset,
            "catalog_visibility": "sunset" if is_sunset else "live",
        }
        if is_sunset:
            sunset_by_agent.append(item)
            if include_sunset:
                by_agent.append(item)
        else:
            by_agent.append(item)
    live_total_cents = sum(int(item["total_cents"]) for item in by_agent if not item["is_sunset"])
    live_total_jobs = sum(int(item["job_count"]) for item in by_agent if not item["is_sunset"])
    sunset_total_cents = sum(int(item["total_cents"]) for item in sunset_by_agent)
    sunset_total_jobs = sum(int(item["job_count"]) for item in sunset_by_agent)
    return JSONResponse(
        content={
            "period": period,
            "days": days,
            "total_cents": int((totals["total_cents"] or 0) if totals else 0),
            "total_jobs": int((totals["job_count"] or 0) if totals else 0),
            "live_catalog_total_cents": live_total_cents,
            "live_catalog_total_jobs": live_total_jobs,
            "sunset_total_cents": sunset_total_cents,
            "sunset_total_jobs": sunset_total_jobs,
            "by_agent": by_agent,
            "sunset_by_agent": sunset_by_agent,
            "include_sunset": include_sunset,
            "note": (
                "by_agent shows live-catalog agents by default. Historical calls "
                "to sunset agents are separated under sunset_by_agent; pass "
                "include_sunset=true to fold them back into by_agent."
            ),
            "wallet_id": wallet_id,
        }
    )


# ---------------------------------------------------------------------------
# Audit endpoint — replaces the client-side aggregation in
# scripts/aztea_mcp_meta_tools.py:_session_audit. Server-side so any MCP
# caller (Python, JS, or raw HTTP) sees the same rich shape regardless of
# which CLI version they have installed. The 2026-05-08 power-user eval
# graded this surface a B− because the CLI route was missing time-range
# filters, bulk Ed25519 verification, and a deterministic digest. Moving
# the logic here also avoids per-receipt HTTP round-trips during bulk
# verify — verify_signature runs in-process.
# ---------------------------------------------------------------------------


@app.get(
    "/wallets/audit",
    responses=_error_responses(401, 403, 422, 429, 500),
    tags=["Wallets"],
    summary="Auditor-grade rollup: spend + signed receipts + optional bulk Ed25519 verification + digest.",
)
@limiter.limit("30/minute")
def wallet_audit(
    request: Request,
    period: str = "1d",
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    verify_all: bool = False,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Single-call audit surface: spend rollup + signed receipts + aggregate
    digest + optional bulk Ed25519 verification across the caller's recent
    completed jobs.

    Query params:
      * ``period``  — ``1d``/``7d``/``30d``/``90d`` (default 1d).
      * ``since``   — ISO-8601 lower bound on settled_at; receipts older are excluded.
      * ``until``   — ISO-8601 upper bound on settled_at; receipts newer are excluded.
      * ``limit``   — receipts to include (1–200, default 100).
      * ``verify_all`` — when true, run Ed25519 verification on every signed
        receipt in the window and return aggregate verified/failed counts +
        first-failure detail. In-process verification, sub-50ms per receipt.

    The ``receipts_digest`` is a SHA-256 fingerprint of (job_id|output_hash|signed)
    rows joined by newline. A different digest on a subsequent poll means
    something material in the audit window changed — caller can pin the
    digest in storage and re-verify on demand without re-walking receipts.
    """
    import hashlib as _hashlib

    _require_scope(caller, "caller")

    period_normalized = str(period or "1d").strip().lower()
    if period_normalized not in {"1d", "7d", "30d", "90d"}:
        period_normalized = "1d"
    days_map = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
    days = days_map[period_normalized]

    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            text = str(value).strip().replace("Z", "+00:00")
            return datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None

    since_dt = _parse_iso(since)
    until_dt = _parse_iso(until)
    job_limit = max(1, min(200, int(limit or 100)))

    period_since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    period_since_iso = period_since_dt.isoformat()

    caller_owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(caller_owner_id)
    wallet_id = wallet["wallet_id"]

    # Spend rollup (mirrors /wallets/spend-summary so callers don't need a
    # second endpoint). Same SQL shape — kept inline rather than refactored
    # into a shared helper because the spend response above also exposes
    # legacy fields (sunset_by_agent, include_sunset) that aren't needed
    # by the audit response.
    with jobs._conn() as conn:
        rows = conn.execute(
            """
            SELECT agent_id, SUM(price_cents) AS total_cents, COUNT(*) AS job_count
            FROM jobs
            WHERE caller_owner_id = %s
              AND status IN ('complete', 'failed', 'stopped')
              AND created_at >= %s
            GROUP BY agent_id
            ORDER BY total_cents DESC
            LIMIT 100
            """,
            (caller_owner_id, period_since_iso),
        ).fetchall()
        totals = conn.execute(
            """
            SELECT SUM(price_cents) AS total_cents, COUNT(*) AS job_count
            FROM jobs
            WHERE caller_owner_id = %s AND created_at >= %s
            """,
            (caller_owner_id, period_since_iso),
        ).fetchone()

        # Receipts: completed jobs ordered by settled_at desc, narrowed by
        # since/until if supplied. We pull a slightly larger window than
        # `limit` so the aggregate digest is stable when the caller
        # paginates (digest covers the same set of receipts each call for
        # the same window).
        receipt_rows = conn.execute(
            """
            SELECT job_id, agent_id, price_cents, caller_charge_cents,
                   settled_at, output_signature, output_signature_alg,
                   output_signed_by_did, output_signed_at, output_payload
            FROM jobs
            WHERE caller_owner_id = %s
              AND status = 'complete'
              AND settled_at IS NOT NULL
            ORDER BY settled_at DESC, job_id DESC
            LIMIT %s
            """,
            (caller_owner_id, job_limit * 2),
        ).fetchall()

    sunset_ids = set(_builtin_constants.SUNSET_DEPRECATED_AGENT_IDS)
    by_agent_live: list[dict[str, Any]] = []
    for row in rows:
        agent_id = row["agent_id"]
        if agent_id in sunset_ids:
            continue
        agent_name = agent_id
        agent_review_status = ""
        if agent_id:
            try:
                ag = registry.get_agent(agent_id, include_unapproved=True)
                if ag:
                    agent_name = ag.get("name") or agent_id
                    agent_review_status = (
                        str(ag.get("review_status") or "").strip().lower()
                    )
            except Exception:
                _LOG.warning(
                    "Failed to resolve agent name for %s in audit", agent_id, exc_info=True
                )
        if agent_review_status == "sunset":
            continue
        by_agent_live.append(
            {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "total_cents": int(row["total_cents"] or 0),
                "job_count": int(row["job_count"] or 0),
            }
        )
    spend = {
        "period": period_normalized,
        "days": days,
        "total_cents": int((totals["total_cents"] or 0) if totals else 0),
        "total_jobs": int((totals["job_count"] or 0) if totals else 0),
        "live_catalog_total_cents": sum(item["total_cents"] for item in by_agent_live),
        "by_agent": by_agent_live,
        "wallet_id": wallet_id,
    }

    # Filter receipts by since/until and shape for response. Receipt
    # rendering matches the existing /jobs/{id}/signature shape so MCP
    # clients can pass any single receipt straight back to verify.
    receipts: list[dict[str, Any]] = []
    for row in receipt_rows:
        settled_at_value = row.get("settled_at")
        settled_dt = _parse_iso(
            settled_at_value.isoformat()
            if hasattr(settled_at_value, "isoformat")
            else settled_at_value
        )
        if since_dt is not None and settled_dt is not None and settled_dt < since_dt:
            continue
        if until_dt is not None and settled_dt is not None and settled_dt > until_dt:
            continue
        # Compute the per-receipt output_hash on the fly so callers don't
        # have to hit /jobs/{id}/signature N times to learn what was
        # signed. The hash is over the canonical-JSON encoding of
        # output_payload (matches what jobs_signature returns).
        output_hash = None
        try:
            from core import crypto as _crypto

            output_hash = _hashlib.sha256(
                _crypto.canonical_json(row.get("output_payload"))
            ).hexdigest()
        except Exception:
            output_hash = None
        signed = bool(row.get("output_signature"))
        receipts.append(
            {
                "job_id": row.get("job_id"),
                "agent_id": row.get("agent_id"),
                "charge_cents": int(
                    (row.get("caller_charge_cents") or row.get("price_cents") or 0)
                ),
                "settled_at": (
                    settled_at_value.isoformat()
                    if hasattr(settled_at_value, "isoformat")
                    else settled_at_value
                ),
                "output_hash": output_hash,
                "signed": signed,
                "signature_alg": row.get("output_signature_alg"),
                "signed_by_did": row.get("output_signed_by_did"),
                "signed_at": (
                    row["output_signed_at"].isoformat()
                    if hasattr(row.get("output_signed_at"), "isoformat")
                    else row.get("output_signed_at")
                ),
                "signature_endpoint": (
                    f"/jobs/{row.get('job_id')}/signature" if signed else None
                ),
            }
        )
        if len(receipts) >= job_limit:
            break

    # Aggregate digest: SHA-256 over a canonical newline-joined string.
    # NOT a Merkle root — receipts are still individually signed by their
    # agents — but it gives auditors a single fingerprint they can pin and
    # detect tampering with without re-walking every receipt.
    digest_lines = [
        f"{r.get('job_id')}|{r.get('output_hash') or ''}|{1 if r.get('signed') else 0}"
        for r in receipts
    ]
    receipts_digest = (
        _hashlib.sha256("\n".join(digest_lines).encode("utf-8")).hexdigest()
        if digest_lines
        else None
    )

    aggregates = {
        "receipts_total": len(receipts),
        "receipts_signed": sum(1 for r in receipts if r.get("signed")),
        "receipts_unsigned": sum(1 for r in receipts if not r.get("signed")),
        "distinct_agents": len(
            {r.get("agent_id") for r in receipts if r.get("agent_id")}
        ),
        "total_settled_cents": sum(int(r.get("charge_cents") or 0) for r in receipts),
        "earliest_settled_at": receipts[-1].get("settled_at") if receipts else None,
        "latest_settled_at": receipts[0].get("settled_at") if receipts else None,
    }

    response: dict[str, Any] = {
        "period": period_normalized,
        "since": since_dt.isoformat() if since_dt else None,
        "until": until_dt.isoformat() if until_dt else None,
        "limit": job_limit,
        "spend": spend,
        "recent_signed_receipts": receipts,
        "receipts_aggregate": aggregates,
        "receipts_digest": receipts_digest,
        "receipts_digest_method": (
            "sha256(job_id|output_hash|signed) joined by newline"
        ),
        "audit_signature_method": (
            "per-job Ed25519 + did:web (call /jobs/{job_id}/signature for any single receipt)"
        ),
        "available_options": {
            "period": "1d | 7d | 30d | 90d (default 1d)",
            "since": "ISO-8601 lower bound on settled_at (e.g. 2026-05-01T00:00:00Z)",
            "until": "ISO-8601 upper bound on settled_at",
            "limit": "1..200 receipts (default 100, sorted newest-first)",
            "verify_all": (
                "true to Ed25519-verify every signed receipt in the window in-process; "
                "returns aggregate verified/failed counts plus first-failure detail"
            ),
        },
    }

    if verify_all:
        # Bulk Ed25519 verification — runs in-process, no HTTP round-trips.
        # Public keys are cached per agent_id within this call so an audit
        # window with N receipts touching K agents loads K public keys, not
        # N. Each verify is sub-50ms; we still cap the call by the receipt
        # window via `limit`, so the worst case is bounded.
        from core import crypto as _crypto
        from core.jobs.db import _decode_json as _decode_payload

        verified = 0
        failed = 0
        first_failure: dict[str, Any] | None = None
        public_key_cache: dict[str, str | None] = {}
        for r, row in zip(receipts, receipt_rows[: len(receipts)]):
            if not r.get("signed"):
                continue
            agent_id = r.get("agent_id") or ""
            if agent_id not in public_key_cache:
                try:
                    ag = registry.get_agent(agent_id, include_unapproved=True)
                    public_key_cache[agent_id] = (
                        ag.get("signing_public_key") if ag else None
                    )
                except Exception:
                    public_key_cache[agent_id] = None
            public_pem = public_key_cache.get(agent_id)
            if not public_pem:
                failed += 1
                if first_failure is None:
                    first_failure = {
                        "job_id": r.get("job_id"),
                        "agent_id": agent_id,
                        "verification_error": "agent_public_key_unavailable",
                    }
                continue
            try:
                # Pre-1.6.9: passed raw `row.get("output_payload")` to
                # verify. On Postgres this is a TEXT column — `crypto`'s
                # `canonical_json` was re-encoding the JSON STRING (escaped
                # quotes, backslashes) instead of the original dict, so
                # the bytes never matched the original signed bytes and
                # every verify returned signature_mismatch. Manual verifies
                # via the public API path worked because that path went
                # through `_row_to_dict` which decoded the JSON. Decode
                # explicitly here so the bytes round-trip cleanly.
                decoded_payload = _decode_payload(
                    row.get("output_payload"), default=None,
                )
                ok = _crypto.verify_signature(
                    public_pem,
                    decoded_payload,
                    row.get("output_signature") or "",
                )
            except Exception as exc:
                failed += 1
                if first_failure is None:
                    first_failure = {
                        "job_id": r.get("job_id"),
                        "agent_id": agent_id,
                        "verification_error": f"verify_raised: {exc!r}",
                    }
                continue
            if ok:
                verified += 1
            else:
                failed += 1
                if first_failure is None:
                    first_failure = {
                        "job_id": r.get("job_id"),
                        "agent_id": agent_id,
                        "verification_error": "signature_mismatch",
                    }
        if verified == 0 and failed == 0:
            verdict = "no_signed_receipts"
        elif failed == 0:
            verdict = "all_verified"
        else:
            verdict = "verification_failed"
        response["bulk_verification"] = {
            "verified": verified,
            "failed": failed,
            "first_failure": first_failure,
            "verdict": verdict,
        }
    else:
        response["bulk_verification_hint"] = (
            "Pass verify_all=true to Ed25519-verify every signed receipt in this window in-process."
        )

    return JSONResponse(content=response)


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
        raise HTTPException(
            status_code=404, detail=f"Wallet '{body.wallet_id}' not found."
        )
    if caller["type"] != "master" and wallet["owner_id"] != caller["owner_id"]:
        raise HTTPException(
            status_code=403, detail="Not authorized to deposit into this wallet."
        )
    if int(body.amount_cents) < MINIMUM_DEPOSIT_CENTS:
        raise _deposit_below_minimum_error(int(body.amount_cents))
    try:
        tx_id = payments.deposit(body.wallet_id, body.amount_cents, body.memo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    wallet = payments.get_wallet(body.wallet_id)
    return JSONResponse(
        content={
            "tx_id": tx_id,
            "wallet_id": body.wallet_id,
            "balance_cents": wallet["balance_cents"],
        }
    )


@app.get(
    "/wallets/me",
    response_model=core_models.WalletResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def wallet_me(
    request: Request,
    limit: int = 50,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletResponse:
    _require_any_scope(caller, "caller", "worker")
    owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(owner_id)
    # Honor the requested transaction page size. Was: silently clamped to 50
    # regardless of `?limit=` (audit S3.8). Cap at 500 so a single response
    # can't blow past the JSON-size budget.
    try:
        page_size = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        page_size = 50
    txs = payments.get_wallet_transactions(wallet["wallet_id"], limit=page_size)
    caller_trust = payments.get_caller_trust(owner_id)

    # 1.6.2: expose escrow_cents alongside balance_cents. The 1.6.1 power-
    # user eval found the CLI showed escrow_cents from `aztea wallet
    # balance --json` but REST `/wallets/me` and MCP `manage_budget(balance)`
    # silently dropped the field. Compute as the sum of caller_charge_cents
    # across the wallet's in-flight jobs — money the caller has authorized
    # but not yet settled or refunded.
    escrow_cents = _compute_escrow_cents(wallet["wallet_id"])

    # Reserve-hold pattern (PR #wallet_holds): expose held_cents +
    # available_cents + the active hold rows. Old consumers continue to
    # read balance_cents unchanged; new SDK / UI consumers should use
    # available_cents for "what can the user spend or withdraw".
    held_cents = int(wallet.get("held_cents") or 0)
    available_cents = max(0, int(wallet["balance_cents"]) - held_cents)
    holds_payload = _list_active_holds_for_wallet(wallet["wallet_id"])

    return JSONResponse(
        content={
            **wallet,
            "held_cents": held_cents,
            "available_cents": available_cents,
            "holds": holds_payload,
            "escrow_cents": escrow_cents,
            "caller_trust": caller_trust,
            "transactions": txs,
        }
    )


def _list_active_holds_for_wallet(wallet_id: str) -> list[dict]:
    """Return the active wallet_holds rows for a wallet, sorted by hold_until.

    Pure read helper for /wallets/me. Returns [] for wallets with no
    holds — including pre-deploy wallets where the column was added but
    never populated.
    """
    if not wallet_id:
        return []
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT hold_id, job_id, amount_cents, hold_until
            FROM wallet_holds
            WHERE wallet_id = %s AND status = 'active'
            ORDER BY hold_until ASC
            LIMIT 200
            """,
            (wallet_id,),
        ).fetchall()
    return [
        {
            "hold_id": str(row["hold_id"]),
            "job_id": str(row["job_id"]),
            "amount_cents": int(row["amount_cents"]),
            "hold_until": str(row["hold_until"]),
        }
        for row in rows
    ]


_ESCROW_JOB_STATES: tuple[str, ...] = (
    "pending",
    "running",
    "awaiting_clarification",
)


def _compute_escrow_cents(wallet_id: str) -> int:
    """Sum of caller_charge_cents across in-flight jobs for ``wallet_id``.

    Pure read — never mutates. Mirrors what the CLI's
    ``aztea wallet balance --json`` has rendered since 1.6.1. Shared by
    /wallets/me (REST) and manage_budget(action=balance) (MCP) so the two
    surfaces agree on the field's meaning and value.
    """
    if not wallet_id:
        return 0
    placeholders = ",".join(["%s"] * len(_ESCROW_JOB_STATES))
    with get_db_connection() as conn:
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(caller_charge_cents), 0) AS escrow_cents
              FROM jobs
             WHERE caller_wallet_id = %s
               AND status IN ({placeholders})
            """,
            (wallet_id, *_ESCROW_JOB_STATES),
        ).fetchone()
    if row is None:
        return 0
    # Postgres returns Decimal for SUM; SQLite returns int. Normalise.
    return int(row["escrow_cents"] or 0)


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


def _enrich_agent_wallet_rows(rows: list[dict]) -> list[dict]:
    """Attach agent_name (and other registry-sourced fields) to wallet breakdown rows.

    Sub-wallets without a matching agent row (orphans from deleted agents)
    keep agent_name = agent_id so they are still visible in the UI.
    """
    enriched = []
    for row in rows:
        agent_id = row.get("agent_id")
        name = agent_id
        if agent_id:
            try:
                agent = registry.get_agent(agent_id, include_unapproved=True)
                if agent:
                    name = agent.get("name") or agent_id
            except (_db.OperationalError, ValueError, TypeError) as exc:
                _LOG.warning(
                    "Failed to load agent name for wallet row %s: %s", agent_id, exc
                )
        enriched.append({**row, "agent_name": name})
    return enriched


@app.get(
    "/wallets/me/agents",
    responses=_error_responses(401, 403, 429, 500),
    tags=["Wallets"],
    summary="Per-agent sub-wallet balances and earnings for the authenticated user.",
)
@limiter.limit("60/minute")
def wallet_me_agents(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
):
    _require_scope(caller, "caller")
    owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(owner_id)
    rows = payments.get_agent_earnings_breakdown_v2(wallet["wallet_id"])
    return JSONResponse(content={"agents": _enrich_agent_wallet_rows(rows)})


@app.get(
    "/wallets/me/agent-earnings",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def wallet_me_agent_earnings(
    request: Request,
    _: core_models.CallerContext = Depends(_require_api_key),
):
    """Compatibility shim: per-agent earnings breakdown.

    The current frontend reads ``earnings[].total_earned_cents`` and
    ``earnings[].call_count``; the v2 aggregator returns those plus
    ``current_balance_cents`` and other sub-wallet metadata. We keep the
    response key name ``earnings`` so older clients keep working.
    """
    owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(owner_id)
    rows = payments.get_agent_earnings_breakdown_v2(wallet["wallet_id"])
    return JSONResponse(content={"earnings": _enrich_agent_wallet_rows(rows)})


def _resolve_owned_agent_wallet(
    request: Request,
    caller: core_models.CallerContext,
    agent_id: str,
) -> dict:
    """Look up the sub-wallet for ``agent_id`` after verifying ownership.

    Raises 404 if the agent does not exist or is not owned by the caller.
    Raises 404 if the sub-wallet's parent does not match the caller's wallet.
    """
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if not agent or agent.get("owner_id") != caller["owner_id"]:
        raise HTTPException(
            status_code=404, detail="Agent not found or you don't own it."
        )
    owner_wallet = payments.get_or_create_wallet(_caller_owner_id(request))
    agent_wallet = payments.get_or_create_wallet(
        f"agent:{agent_id}",
        parent_wallet_id=owner_wallet["wallet_id"],
        display_label=agent.get("name") or None,
    )
    # Defence-in-depth: even if the wallet was created before the migration,
    # confirm the parent link matches the authenticated user.
    parent = agent_wallet.get("parent_wallet_id")
    if parent and parent != owner_wallet["wallet_id"]:
        raise HTTPException(
            status_code=404, detail="Agent wallet is not linked to your account."
        )
    return agent_wallet


@app.get(
    "/wallets/agents/{agent_id}/transactions",
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Wallets"],
    summary="Recent transactions on one of your agent sub-wallets.",
)
@limiter.limit("60/minute")
def wallet_agent_transactions(
    request: Request,
    agent_id: str,
    limit: int = 50,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    agent_wallet = _resolve_owned_agent_wallet(request, caller, agent_id)
    capped = max(1, min(int(limit or 50), 200))
    txs = payments.get_wallet_transactions(agent_wallet["wallet_id"], limit=capped)
    return JSONResponse(
        content={
            "wallet_id": agent_wallet["wallet_id"],
            "agent_id": agent_id,
            "transactions": txs,
        }
    )


@app.patch(
    "/wallets/agents/{agent_id}/settings",
    responses=_error_responses(400, 401, 403, 404, 429, 500),
    tags=["Wallets"],
    summary="Update label, daily spend limit, and guarantor policy for an agent's sub-wallet.",
)
@limiter.limit("30/minute")
def wallet_agent_settings_update(
    request: Request,
    agent_id: str,
    body: core_models.AgentWalletSettingsRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    agent_wallet = _resolve_owned_agent_wallet(request, caller, agent_id)
    wallet_id = agent_wallet["wallet_id"]

    try:
        if body.display_label is not None:
            payments.set_wallet_label(wallet_id, body.display_label)
        if (
            body.daily_spend_limit_cents is not None
            or body.daily_spend_limit_cents == 0
        ):
            # Allow None semantics via explicit field absence; here we mirror the existing
            # set_wallet_daily_spend_limit signature which accepts int or None.
            payments.set_wallet_daily_spend_limit(
                wallet_id, body.daily_spend_limit_cents
            )
        if body.guarantor_enabled is not None or body.guarantor_cap_cents is not None:
            # Read current values so partial updates preserve the other field.
            current = payments.get_wallet(wallet_id) or {}
            enabled = (
                body.guarantor_enabled
                if body.guarantor_enabled is not None
                else bool(current.get("guarantor_enabled"))
            )
            cap = (
                body.guarantor_cap_cents
                if body.guarantor_cap_cents is not None
                else current.get("guarantor_cap_cents")
            )
            payments.set_wallet_guarantor(wallet_id, enabled=enabled, cap_cents=cap)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    refreshed = payments.get_wallet(wallet_id) or {}
    return JSONResponse(
        content={
            "wallet_id": wallet_id,
            "agent_id": agent_id,
            "display_label": refreshed.get("display_label"),
            "daily_spend_limit_cents": refreshed.get("daily_spend_limit_cents"),
            "guarantor_enabled": bool(refreshed.get("guarantor_enabled")),
            "guarantor_cap_cents": refreshed.get("guarantor_cap_cents"),
        }
    )


@app.post(
    "/wallets/agents/{agent_id}/sweep",
    responses=_error_responses(400, 401, 403, 404, 429, 500),
    tags=["Wallets"],
    summary="Move funds from an agent's sub-wallet back to the owner's wallet.",
)
@limiter.limit("20/minute")
def wallet_agent_sweep(
    request: Request,
    agent_id: str,
    body: core_models.AgentWalletSweepRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    agent_wallet = _resolve_owned_agent_wallet(request, caller, agent_id)
    try:
        result = payments.sweep_to_parent(
            agent_wallet["wallet_id"],
            amount_cents=body.amount_cents,
            memo=f"sweep from agent {agent_id}",
        )
    except payments.InsufficientBalanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "wallet_id": agent_wallet["wallet_id"],
            **result,
        }
    )


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
        return JSONResponse(
            content={"runs": [], "skipped_lines": 0, "skipped_line_numbers": []}
        )
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
    return JSONResponse(
        {
            "stripe_enabled": bool(_STRIPE_SECRET_KEY and _STRIPE_AVAILABLE),
            "stripe_publishable_key": _STRIPE_PUBLISHABLE_KEY or None,
        }
    )


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
            "summary": item.get("summary") or "",
            "category": item.get("category") or "Reference",
            "path": f"/public/docs/{item['slug']}",
        }
        for item in entries
    ]
    return JSONResponse({"docs": docs, "count": len(docs)})


@app.post(
    "/public/docs/ask",
    tags=["docs"],
    summary="Ask an AI question grounded in a documentation page.",
)
@limiter.limit("20/minute", key_func=get_remote_address)
def public_docs_ask(request: Request, body: dict) -> JSONResponse:
    question = str(body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required.")
    if len(question) > 1000:
        raise HTTPException(
            status_code=400, detail="question must be 1000 characters or fewer."
        )

    doc_slug = str(body.get("doc_slug") or "").strip()
    citations: list[dict] = []
    context_parts: list[str] = []

    primary_doc = _find_public_doc(doc_slug) if doc_slug else None
    if primary_doc:
        try:
            with open(primary_doc["full_path"], encoding="utf-8") as fh:
                context_parts.append(
                    f"# {primary_doc.get('title') or primary_doc['slug']} ({primary_doc['slug']})\n"
                    + fh.read()[:8000]
                )
            citations.append(
                {
                    "slug": primary_doc["slug"],
                    "title": primary_doc.get("title") or primary_doc["slug"],
                }
            )
        except OSError:
            primary_doc = None

    # Always include 1-2 additional likely-relevant docs scored by token overlap with question.
    all_entries = _public_docs_entries()
    q_tokens = {t for t in question.lower().split() if len(t) > 2}
    scored = []
    for entry in all_entries:
        if primary_doc and entry["slug"] == primary_doc["slug"]:
            continue
        title = (entry.get("title") or "").lower()
        summary = (entry.get("summary") or "").lower()
        slug = entry["slug"].lower()
        score = sum(1 for t in q_tokens if t in title or t in summary or t in slug)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda x: -x[0])
    for _, entry in scored[:2]:
        try:
            with open(entry["full_path"], encoding="utf-8") as fh:
                context_parts.append(
                    f"# {entry.get('title') or entry['slug']} ({entry['slug']})\n"
                    + fh.read()[:4000]
                )
            citations.append(
                {"slug": entry["slug"], "title": entry.get("title") or entry["slug"]}
            )
        except OSError:
            pass

    if not context_parts:
        for entry in all_entries[:6]:
            try:
                with open(entry["full_path"], encoding="utf-8") as fh:
                    context_parts.append(
                        f"# {entry.get('title') or entry['slug']} ({entry['slug']})\n"
                        + fh.read()[:2500]
                    )
                citations.append(
                    {
                        "slug": entry["slug"],
                        "title": entry.get("title") or entry["slug"],
                    }
                )
            except OSError:
                pass

    context_text = "\n\n---\n\n".join(context_parts)[:14000]

    system_prompt = (
        "You are the documentation assistant for the Aztea platform. Answer using ONLY the "
        "documentation provided. If the answer is not in the docs, say so plainly.\n\n"
        "Formatting rules (strict):\n"
        "- Output GitHub-flavored Markdown.\n"
        "- For ANY shell command, code snippet, file path, or multi-token identifier, use a "
        "fenced code block with a language tag, e.g. ```bash, ```python, ```json. "
        "NEVER place commands or code on a bare line without a fence.\n"
        "- Use single backticks ONLY for short inline tokens (a function name, env var, or flag).\n"
        "- Use `##` headings (not bold) to separate sections like `## Using the Aztea CLI`.\n"
        "- Keep prose tight: under 250 words total. Prefer code over prose when illustrating usage."
    )
    user_msg = f"Documentation:\n{context_text}\n\nQuestion: {question}"
    try:
        from core.llm import CompletionRequest as _CR
        from core.llm import Message as _Msg
        from core.llm import run_with_fallback as _rwf

        raw = _rwf(
            _CR(
                model="",
                messages=[
                    _Msg(role="system", content=system_prompt),
                    _Msg(role="user", content=user_msg),
                ],
                temperature=0.2,
                max_tokens=700,
            )
        )
        answer = raw.text.strip()
    except Exception as exc:
        _LOG.warning("docs/ask LLM failure: %s", exc)
        raise HTTPException(
            status_code=503, detail="AI service temporarily unavailable."
        ) from None
    return JSONResponse({"answer": answer, "citations": citations})


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
        raise HTTPException(
            status_code=500, detail="Unable to read documentation file."
        ) from None
    return JSONResponse(
        {
            "slug": doc["slug"],
            "title": doc["title"],
            "summary": doc.get("summary") or "",
            "category": doc.get("category") or "Reference",
            "content": content,
        }
    )


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
    if (
        code in {"insufficient_funds", "balance_insufficient"}
        or "insufficient" in message
    ):
        return 400, {
            "error": "payment.stripe_insufficient_funds",
            "message": "Payouts are temporarily unavailable because Stripe platform balance is insufficient.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if (
        code in {"account_closed", "account_invalid", "no_such_destination"}
        or "no such destination" in message
    ):
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
        if not _db.IS_POSTGRES:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
        processed_row = conn.execute(
            "SELECT 1 FROM stripe_sessions WHERE session_id = %s LIMIT 1",
            (session_id,),
        ).fetchone()
        if processed_row is not None:
            conn.commit()
            return "already_processed"
        state_row = conn.execute(
            """
            SELECT status
            FROM stripe_webhook_events
            WHERE session_id = %s
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if state_row is None:
            conn.execute(
                """
                INSERT INTO stripe_webhook_events
                    (session_id, wallet_id, amount_cents, status, attempts, created_at, updated_at)
                VALUES (%s, %s, %s, 'processing', 1, %s, %s)
                """,
                (session_id, wallet_id, int(amount_cents), now, now),
            )
            conn.commit()
            return "acquired"
        status = str(state_row.get("status", "") or "").strip().lower()
        if status == "processed":
            conn.commit()
            return "already_processed"
        if status == "processing":
            conn.commit()
            return "already_processing"
        conn.execute(
            """
            UPDATE stripe_webhook_events
            SET wallet_id = %s,
                amount_cents = %s,
                status = 'processing',
                attempts = attempts + 1,
                last_error = NULL,
                updated_at = %s
            WHERE session_id = %s
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
        if not _db.IS_POSTGRES:
            conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            UPDATE stripe_webhook_events
            SET status = 'failed',
                last_error = %s,
                updated_at = %s
            WHERE session_id = %s
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
        if not _db.IS_POSTGRES:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO stripe_sessions (session_id, wallet_id, amount_cents, processed_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (session_id, wallet_id, int(amount_cents), now),
        )
        conn.execute(
            """
            UPDATE stripe_webhook_events
            SET status = 'processed',
                last_error = NULL,
                updated_at = %s
            WHERE session_id = %s
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
            WHERE wallet_id = %s AND processed_at >= %s
            """,
            (wallet_id, window_start),
        ).fetchone()
    if row is None:
        return 0
    return int(row.get("total") or 0)


# ---------------------------------------------------------------------------
# Admin platform earnings — view balances + withdraw into admin's own wallet.
# Covers two pools:
#   • owner_id="platform"   → 10% platform fee on every agent call
#   • owner_id=f"user:{system_user_id}" → 90% payout to built-in agents (owned
#     by the system user that runs every internal://... agent in the registry)
# ---------------------------------------------------------------------------


def _admin_earnings_pools() -> dict[str, dict]:
    """Resolve the platform treasury and the built-in-agents (system user) wallets."""
    system_user_id = _ensure_system_user()
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    system_wallet = payments.get_or_create_wallet(f"user:{system_user_id}")
    return {
        "platform": platform_wallet,
        "system_agents": system_wallet,
    }


@app.get(
    "/admin/platform/earnings",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def admin_platform_earnings(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Return balances + recent ledger entries for both platform pools."""
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    pools = _admin_earnings_pools()
    payload = {}
    for source_key, wallet in pools.items():
        txs = payments.get_wallet_transactions(wallet["wallet_id"], limit=50)
        payload[source_key] = {
            "owner_id": wallet["owner_id"],
            "wallet_id": wallet["wallet_id"],
            "balance_cents": int(wallet.get("balance_cents") or 0),
            "recent_transactions": txs,
        }
    return JSONResponse(content=payload)


@app.post(
    "/admin/platform/withdraw",
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("10/minute")
def admin_platform_withdraw(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Transfer funds from a platform pool into the authenticated admin's own wallet.

    Body: ``{ "source": "platform"|"system_agents", "amount_cents": int, "memo"%s: str }``
    """
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    if caller["type"] != "user":
        raise HTTPException(
            status_code=403, detail="Admin withdrawals require a user-scoped key."
        )

    source_key = str((body or {}).get("source") or "").strip().lower()
    pools = _admin_earnings_pools()
    if source_key not in pools:
        raise HTTPException(
            status_code=400, detail="source must be 'platform' or 'system_agents'."
        )
    try:
        amount_cents = int((body or {}).get("amount_cents") or 0)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="amount_cents must be a positive integer."
        )
    if amount_cents <= 0:
        raise HTTPException(
            status_code=400, detail="amount_cents must be a positive integer."
        )
    memo = (
        str((body or {}).get("memo") or "").strip()[:240]
        or f"Admin withdrawal from {source_key}"
    )

    src_wallet = pools[source_key]
    admin_user_id = caller["user"]["user_id"]
    dest_wallet = payments.get_or_create_wallet(admin_user_id)

    try:
        result = payments.admin_transfer(
            src_wallet["wallet_id"],
            dest_wallet["wallet_id"],
            amount_cents,
            memo=memo,
        )
    except payments.InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Insufficient balance: pool has {exc.balance_cents}¢, requested {exc.required_cents}¢.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return JSONResponse(
        content={
            "source": source_key,
            "transferred_cents": result["amount_cents"],
            "debit_tx_id": result["debit_tx_id"],
            "credit_tx_id": result["credit_tx_id"],
            "admin_wallet_id": dest_wallet["wallet_id"],
        }
    )


# ---------------------------------------------------------------------------
# Watchers — minimal JSON discovery surface (1.6.1)
# ---------------------------------------------------------------------------
# The watcher CRUD module shipped with PR #15 / #16 but never gained a public
# JSON route. Tools probing /watchers, /api/watchers, /v1/watchers all hit the
# SPA fallback and got HTML. This GET makes the surface discoverable; mutating
# operations stay in the dedicated owner-scoped routes (kept in core/watchers
# for now until the schema stabilises further).

@app.get(
    "/watchers",
    responses=_error_responses(401, 403, 429, 500),
    tags=["Watchers"],
    summary="List watchers owned by the calling user.",
)
@limiter.limit("60/minute")
def watchers_list(
    request: Request,
    limit: int = 100,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_any_scope(caller, "caller", "worker")
    from core import watchers as _watchers

    owner_id = caller.get("owner_id") or ""
    rows = _watchers.crud.list_watchers_for_owner(owner_id, limit=int(limit))
    return JSONResponse(content={"watchers": rows, "count": len(rows)})


# 1.6.2: POST /watchers — the GET shipped with PR #15/#16 but the create
# verb returned "405 Method Not Allowed" in the 1.6.1 power-user eval.
# Wire it up so the CLAUDE.md-documented contract is real.
class _WatcherCreateRequest(BaseModel):
    """Watchers poll a URL/manifest/repo (or just tick on a cron) and fire
    ``agent_id``. ``target_url`` is the resource being watched, NOT the agent
    endpoint. 1.7.9 — added 'cron' kind for tick-driven watchers (no external
    resource to fingerprint); previously the inline ``target_url=Field(...)``
    rejected every cron-only create with 422 even though core/watchers/models.py
    accepted it (eval B-17, four releases).

    Example (cron — fires every tick)::

        {
          "agent_id": "<uuid>",
          "target_kind": "cron",
          "tick_interval_seconds": 3600,
          "budget_per_day_cents": 100,
          "delivery_email": "you@example.com",
          "payload": {"task": "fetch overnight news"}
        }

    Example (http — fires on body change)::

        {
          "agent_id": "<uuid>",
          "target_kind": "http",
          "target_url": "https://example.com/changelog",
          "tick_interval_seconds": 300,
          "budget_per_day_cents": 100,
          "payload": {"task": "summarize the diff"}
        }
    """
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "agent_id": "00000000-0000-0000-0000-000000000001",
                "target_kind": "http",
                "target_url": "https://news.ycombinator.com/rss",
                "tick_interval_seconds": 600,
                "budget_per_day_cents": 50,
                "payload": {"task": "summarize new HN front-page items"},
            }
        },
    )

    agent_id: str = Field(..., description="UUID of the registry agent to fire on change.")
    target_kind: Literal["http", "manifest", "git", "cron"] = Field(
        default="http",
        description=(
            "Target type. 'http' | 'manifest' | 'git' poll a resource and "
            "fire on fingerprint change. 'cron' fires every tick with no "
            "fingerprint comparison (target_url is not used)."
        ),
    )
    # 1.7.9 — optional. Conditional validation in the model_validator below
    # mirrors core/watchers/models.py: required for http/git/manifest, ignored
    # for cron. Pre-1.7.9 this was Field(...) which 422'd every cron-only
    # create regardless of what the conditional check downstream said.
    target_url: str | None = Field(
        default=None,
        description=(
            "URL or registry path the watcher polls for change fingerprinting. "
            "Required for kind in {'http','git','manifest'}. Ignored for "
            "kind='cron' (cron watchers fire every tick — no resource to poll)."
        ),
    )
    target_meta: dict | None = Field(
        default=None,
        description="Type-specific metadata (e.g. {'ecosystem':'npm','package':'react'}).",
    )
    on_change_policy: str = Field(
        default="fire_once_per_change",
        description=(
            "When to fire: 'fire_once_per_change' | 'fire_on_every_tick'. "
            "For target_kind='cron' this is auto-corrected to 'fire_on_every_tick'."
        ),
    )
    tick_interval_seconds: int = Field(
        default=3600, ge=60, le=86400,
        description="Poll cadence in seconds. Min 60s (anti-abuse), max 24h.",
    )
    budget_per_day_cents: int = Field(
        default=100, ge=1, le=10000,
        description="Hard cap on agent-call spend per 24h. Watcher pauses on overrun.",
    )
    delivery_webhook_url: str | None = Field(
        default=None, description="Optional webhook URL to POST job results to.",
    )
    delivery_email: str | None = Field(
        default=None, description="Optional email address to send job results to.",
    )
    payload: dict | None = Field(
        default=None, description="Input payload passed to the agent on each fire.",
    )

    @model_validator(mode="after")
    def _check_target_url(self) -> "_WatcherCreateRequest":
        """Conditional target_url requirement (1.7.9 fix for B-17).

        Mirrors the validation in core/watchers/models.py::WatcherCreate so
        the wire-level model and the persistence-level model agree. Only
        http / git / manifest kinds need a polled resource; cron is a pure
        tick driver.
        """
        if self.target_kind in ("http", "git", "manifest"):
            if not (self.target_url and self.target_url.strip()):
                raise ValueError(
                    f"target_url is required when target_kind is "
                    f"'{self.target_kind}'."
                )
        return self


@app.post(
    "/watchers",
    status_code=201,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
    tags=["Watchers"],
    summary="Create a watcher (poll + fire-on-change).",
)
@limiter.limit("30/minute")
def watchers_create(
    request: Request,
    body: _WatcherCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Create a new watcher row owned by the calling user.

    The watcher sweeper picks it up on the next tick, fingerprints the
    target, and fires the agent when the fingerprint changes (subject to
    the daily budget). Update / delete remain in the dedicated owner-
    scoped routes alongside this one.
    """
    _require_any_scope(caller, "caller", "worker")
    from core import watchers as _watchers

    owner_id = str(caller.get("owner_id") or "").strip()
    if not owner_id:
        raise HTTPException(status_code=403, detail="Caller has no owner_id.")

    # The watcher sweeper bills agent fires against the caller's primary
    # wallet — same model as a direct hire — so the daily-budget enforcer
    # and audit log roll up correctly.
    wallet = payments.get_or_create_wallet(owner_id)
    caller_wallet_id = str(wallet.get("wallet_id") or "")
    if not caller_wallet_id:
        raise HTTPException(
            status_code=500,
            detail="Could not resolve a wallet for the calling user.",
        )

    # SSRF check on the watcher target — the sweeper hits this URL directly
    # when fingerprinting, so private/loopback targets must be blocked at
    # create time, not deferred to the first tick.
    if str(body.target_kind).strip().lower() == "http":
        try:
            _url_security.validate_outbound_url(body.target_url, "target_url")
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    "watchers.target_url_blocked",
                    str(exc),
                    {"target_url": body.target_url},
                ),
            )

    try:
        watcher = _watchers.crud.create_watcher(
            owner_user_id=owner_id,
            caller_wallet_id=caller_wallet_id,
            agent_id=body.agent_id,
            target_kind=body.target_kind,
            target_url=body.target_url,
            target_meta=body.target_meta,
            on_change_policy=body.on_change_policy,
            tick_interval_seconds=int(body.tick_interval_seconds),
            budget_per_day_cents=int(body.budget_per_day_cents),
            delivery_webhook_url=body.delivery_webhook_url,
            delivery_email=body.delivery_email,
            payload=body.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return JSONResponse(
        status_code=201,
        content={
            "watcher_id": watcher.get("watcher_id"),
            "agent_id": watcher.get("agent_id"),
            "target_kind": watcher.get("target_kind"),
            "target_url": watcher.get("target_url"),
            "tick_interval_seconds": watcher.get("tick_interval_seconds"),
            "budget_per_day_cents": watcher.get("budget_per_day_cents"),
            "next_check_at": watcher.get("next_check_at"),
            "status": watcher.get("status"),
            "message": (
                "Watcher created. The sweeper will fingerprint the target on "
                "its next pass and fire the agent on observed change."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Caller self-reconcile (1.6.1)
# ---------------------------------------------------------------------------
# /ops/payments/reconcile is admin-only (system-wide audit). Callers couldn't
# verify their own wallet drift without root creds. This endpoint lets a
# caller-scoped key reconcile only their own wallet.

@app.get(
    "/wallets/me/reconcile",
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Wallets"],
    summary="Reconcile the caller's own wallet (cached balance vs ledger sum).",
)
# 1.7.0: lowered from 10/minute to 6/minute. Burst test (100 parallel
# requests) saw 55 × 429, 33 × 502, 12 × 200. The 502s came from uvicorn
# workers dropping connections under contention — not the rate limiter.
# Tightening the limit pushes more bursts into the 429 lane (clean retry)
# and out of the 502 lane (caller has to guess what happened).
@limiter.limit("6/minute")
def wallet_self_reconcile(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_any_scope(caller, "caller", "worker")
    owner_id = caller.get("owner_id") or ""
    wallet = payments.get_or_create_wallet(owner_id)
    wallet_id = wallet["wallet_id"]
    cached = int(wallet.get("balance_cents") or 0)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS net
            FROM transactions
            WHERE wallet_id = %s
            """,
            (wallet_id,),
        ).fetchone()
    ledger = int((dict(row) if row else {}).get("net") or 0)
    drift = cached - ledger
    return JSONResponse(
        content={
            "wallet_id": wallet_id,
            "cached_balance_cents": cached,
            "ledger_sum_cents": ledger,
            "drift_cents": drift,
            "invariant_ok": drift == 0,
        }
    )
