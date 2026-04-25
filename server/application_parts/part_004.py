# server.application shard 4 — built-in agent execution (no-HTTP routing for
# internal agents), builtin worker + dispute judge + endpoint health +
# payments reconciliation + hook-delivery background loops, and the job
# callback / hook delivery pipeline. No HTTP routes here.


from server.pricing_helpers import (  # noqa: E402
    builtin_pricing_overlay as _builtin_pricing_overlay,  # noqa: F401
    resolve_agent_pricing as _resolve_agent_pricing,  # noqa: F401
    estimate_variable_charge as _estimate_variable_charge,  # noqa: F401
    maybe_refund_pricing_diff as _maybe_refund_pricing_diff,  # noqa: F401
)


def _execute_builtin_agent(agent_id: str, input_payload: dict[str, Any]) -> dict:
    payload = input_payload or {}
    if agent_id == _FINANCIAL_AGENT_ID:
        body = FinancialRequest.model_validate(payload)
        return _invoke_financial_agent(body)
    if agent_id == _CODEREVIEW_AGENT_ID:
        body = CodeReviewRequest.model_validate(payload)
        return _invoke_code_review_agent(body)
    if agent_id == _WIKI_AGENT_ID:
        body = WikiRequest.model_validate(payload)
        return _invoke_wiki_agent(body)
    if agent_id == _QUALITY_JUDGE_AGENT_ID:
        return judges.run_quality_judgment(
            input_payload=payload.get("input_payload") if isinstance(payload, dict) else {},
            output_payload=payload.get("output_payload") if isinstance(payload, dict) else {},
            agent_description=str(payload.get("agent_description") or "") if isinstance(payload, dict) else "",
        )
    if agent_id == _CVELOOKUP_AGENT_ID:
        return agent_cve_lookup.run(payload)
    if agent_id == _IMAGE_GENERATOR_AGENT_ID:
        return agent_image_generator.run(payload)
    if agent_id == _VIDEO_STORYBOARD_AGENT_ID:
        return agent_video_storyboard.run(payload)
    if agent_id == _ARXIV_RESEARCH_AGENT_ID:
        return agent_arxiv_research.run(payload)
    if agent_id == _PYTHON_EXECUTOR_AGENT_ID:
        return agent_python_executor.run(payload)
    if agent_id == _WEB_RESEARCHER_AGENT_ID:
        return agent_web_researcher.run(payload)
    if agent_id == _GITHUB_FETCHER_AGENT_ID:
        return agent_github_fetcher.run(payload)
    if agent_id == _HN_DIGEST_AGENT_ID:
        return agent_hn_digest.run(payload)
    if agent_id == _DNS_INSPECTOR_AGENT_ID:
        return agent_dns_inspector.run(payload)
    raise ValueError(f"Unsupported built-in agent '{agent_id}'.")


def _process_pending_builtin_job(job: dict) -> bool:
    claimed = jobs.claim_job(
        job["job_id"],
        claim_owner_id=_BUILTIN_WORKER_OWNER_ID,
        lease_seconds=_DEFAULT_LEASE_SECONDS,
        require_authorized_owner=False,
    )
    if claimed is None:
        return False

    _record_job_event(
        claimed,
        "job.claimed",
        actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
        payload={
            "lease_seconds": _DEFAULT_LEASE_SECONDS,
            "attempt_count": claimed["attempt_count"],
            "auto_worker": True,
        },
    )
    jobs.add_message(
        claimed["job_id"],
        from_id=_BUILTIN_WORKER_OWNER_ID,
        msg_type="progress",
        payload={"message": "Built-in worker started processing.", "percent": 5},
    )

    agent_for_run = registry.get_agent(claimed["agent_id"], include_unapproved=True)
    is_hosted_skill = bool(
        agent_for_run is not None
        and _hosted_skills.is_skill_endpoint(agent_for_run.get("endpoint_url"))
    )

    def _heartbeat() -> None:
        jobs.heartbeat_job_lease(
            claimed["job_id"],
            claim_owner_id=_BUILTIN_WORKER_OWNER_ID,
            lease_seconds=_DEFAULT_LEASE_SECONDS,
            claim_token=claimed.get("claim_token"),
            require_authorized_owner=False,
        )

    try:
        if is_hosted_skill:
            skill_row = _hosted_skills.get_hosted_skill_by_agent_id(str(claimed["agent_id"]))
            if skill_row is None:
                raise RuntimeError("Hosted skill record is missing.")
            output = _skill_executor.execute_hosted_skill(
                skill_row,
                claimed.get("input_payload") or {},
                heartbeat_cb=_heartbeat,
            )
        else:
            output = _execute_builtin_agent(
                str(claimed["agent_id"]),
                claimed.get("input_payload") or {},
            )
    except _groq.RateLimitError as exc:
        retried = jobs.schedule_job_retry(
            claimed["job_id"],
            retry_delay_seconds=_SWEEPER_RETRY_DELAY_SECONDS,
            error_message=f"Built-in worker rate-limited: {exc}",
            claim_owner_id=_BUILTIN_WORKER_OWNER_ID,
            claim_token=claimed.get("claim_token"),
            require_authorized_owner=False,
        )
        if retried is not None and retried["status"] == "pending":
            _record_job_event(
                retried,
                "job.retry_scheduled",
                actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                payload={"retry_count": retried["retry_count"], "next_retry_at": retried["next_retry_at"]},
            )
            return True
        updated = retried or jobs.update_job_status(
            claimed["job_id"],
            "failed",
            error_message=f"Built-in worker rate-limited: {exc}",
            completed=True,
        )
        if updated is not None:
            _settle_failed_job(
                updated,
                actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                event_type="job.failed_builtin",
            )
        return True
    except Exception as exc:
        updated = jobs.update_job_status(
            claimed["job_id"],
            "failed",
            error_message=f"Built-in execution failed: {exc}",
            completed=True,
        )
        if updated is not None:
            _settle_failed_job(
                updated,
                actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                event_type="job.failed_builtin",
            )
        return True

    jobs.add_message(
        claimed["job_id"],
        from_id=_BUILTIN_WORKER_OWNER_ID,
        msg_type="final_result",
        payload={"message": "Built-in worker completed successfully."},
    )
    agent = registry.get_agent(claimed["agent_id"], include_unapproved=True)
    if agent is not None:
        output_schema = agent.get("output_schema")
        if isinstance(output_schema, dict) and output_schema:
            mismatches = _validate_json_schema_subset(output, output_schema)
            if mismatches:
                updated = jobs.update_job_status(
                    claimed["job_id"],
                    "failed",
                    error_message=f"Output schema mismatch: {', '.join(mismatches[:3])}",
                    completed=True,
                )
                if updated is not None:
                    _settle_failed_job(
                        updated,
                        actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                        event_type="job.failed_schema",
                    )
                return True
        quality = _run_quality_gate(claimed, agent, output)
        jobs.set_job_quality_result(
            claimed["job_id"],
            judge_verdict=quality["judge_verdict"],
            quality_score=quality["quality_score"],
            judge_agent_id=quality["judge_agent_id"],
        )
        if not quality["passed"]:
            updated = jobs.update_job_status(
                claimed["job_id"],
                "failed",
                error_message=f"Quality judge failed: {quality['reason']}",
                completed=True,
            )
            if updated is not None:
                _settle_failed_job(
                    updated,
                    actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                    event_type="job.failed_quality",
                )
            return True
    completed = jobs.update_job_status(
        claimed["job_id"],
        "complete",
        output_payload=output,
        completed=True,
    )
    if completed is not None:
        settled = _settle_successful_job(completed, actor_owner_id=_BUILTIN_WORKER_OWNER_ID)
        if agent is not None:
            distribution = payments.compute_success_distribution(
                int(completed.get("price_cents") or 0),
                platform_fee_pct=completed.get("platform_fee_pct_at_create"),
                fee_bearer_policy=completed.get("fee_bearer_policy"),
            )
            platform_fee_cents = int(distribution["platform_fee_cents"])
            judge_fee_cents = min(_JUDGE_FEE_CENTS, platform_fee_cents)
            if judge_fee_cents > 0:
                judge_agent_id = str(settled.get("judge_agent_id") or _QUALITY_JUDGE_AGENT_ID)
                judge_wallet = payments.get_or_create_wallet(f"agent:{judge_agent_id}")
                payments.record_judge_fee(
                    completed["platform_wallet_id"],
                    judge_wallet["wallet_id"],
                    charge_tx_id=completed["charge_tx_id"],
                    agent_id=completed["agent_id"],
                    fee_cents=judge_fee_cents,
                )
    return True


def _process_pending_builtin_jobs(limit_per_agent: int = _BUILTIN_JOB_WORKER_BATCH_SIZE) -> dict[str, int]:
    batch_limit = min(max(1, int(limit_per_agent)), 500)
    scanned = 0
    processed = 0
    skill_agent_ids = set(_hosted_skills.list_pending_skill_agent_ids())
    target_agent_ids = list(_BUILTIN_AGENT_IDS) + [
        aid for aid in skill_agent_ids if aid not in _BUILTIN_AGENT_IDS
    ]
    for agent_id in target_agent_ids:
        pending = jobs.list_jobs_for_agent(
            agent_id,
            status="pending",
            limit=batch_limit,
        )
        scanned += len(pending)
        for job in pending:
            if _process_pending_builtin_job(job):
                processed += 1
    return {"scanned": scanned, "processed": processed}


def _builtin_worker_loop(stop_event: threading.Event) -> None:
    _set_builtin_worker_state(running=True, started_at=_utc_now_iso())
    while not stop_event.wait(_BUILTIN_JOB_WORKER_INTERVAL_SECONDS):
        started = _utc_now_iso()
        try:
            summary = _process_pending_builtin_jobs(limit_per_agent=_BUILTIN_JOB_WORKER_BATCH_SIZE)
            _set_builtin_worker_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
        except Exception as exc:
            _LOG.exception("Built-in worker loop failed.")
            _set_builtin_worker_state(
                last_run_at=started,
                last_error=str(exc),
            )
    _set_builtin_worker_state(running=False)


_TIE_TIMEOUT_HOURS = 48
_TIE_TIMEOUT_REASONING = (
    "Judges tied after two rounds. Defaulting to caller per platform policy."
)


def _run_pending_dispute_judgments(limit: int = 100, actor_owner_id: str = "system:dispute-judge") -> dict:
    capped = min(max(1, int(limit)), 500)
    pending = disputes.list_disputes(status="pending", limit=capped)
    judged_count = 0
    resolved_count = 0
    tied_count = 0
    tie_timeout_count = 0
    errors: list[dict[str, str]] = []
    processed_ids: list[str] = []
    resolved_ids: list[str] = []
    tied_ids: list[str] = []

    for dispute_row in pending:
        dispute_id = str(dispute_row.get("dispute_id") or "").strip()
        if not dispute_id:
            continue
        try:
            latest, _ = _resolve_dispute_with_judges(dispute_id, actor_owner_id=actor_owner_id)
        except Exception as exc:
            errors.append({"dispute_id": dispute_id, "error": str(exc)})
            continue
        judged_count += 1
        processed_ids.append(dispute_id)
        status = str(latest.get("status") or "").strip().lower()
        if status == "resolved":
            resolved_count += 1
            resolved_ids.append(dispute_id)
        elif status == "tied":
            tied_count += 1
            tied_ids.append(dispute_id)

    # Auto-rule tied disputes older than _TIE_TIMEOUT_HOURS in favour of caller.
    stale_tied = disputes.get_stale_tied_disputes(older_than_hours=_TIE_TIMEOUT_HOURS, limit=capped)
    for dispute_row in stale_tied:
        dispute_id = str(dispute_row.get("dispute_id") or "").strip()
        if not dispute_id:
            continue
        try:
            disputes.record_judgment(
                dispute_id,
                judge_kind="human_admin",
                verdict="caller_wins",
                reasoning=_TIE_TIMEOUT_REASONING,
                model=None,
                admin_user_id="system_tie_timeout",
            )
            payments.post_dispute_settlement(
                dispute_id,
                outcome="caller_wins",
            )
            finalized = disputes.finalize_dispute(
                dispute_id,
                status="final",
                outcome="caller_wins",
            )
            if finalized is not None:
                _apply_dispute_effects(finalized, "caller_wins")
                job = jobs.get_job(finalized["job_id"])
                if job is not None:
                    _record_job_event(
                        job,
                        "job.dispute_finalized",
                        actor_owner_id=actor_owner_id,
                        payload={"dispute_id": dispute_id, "outcome": "caller_wins", "reason": "tie_timeout"},
                    )
            tie_timeout_count += 1
            _LOG.warning(
                "Tie-timeout auto-ruling: dispute %s → caller_wins after %dh",
                dispute_id,
                _TIE_TIMEOUT_HOURS,
            )
        except Exception as exc:
            errors.append({"dispute_id": dispute_id, "error": f"tie_timeout: {exc}"})

    return {
        "pending_scanned": len(pending),
        "judged_count": judged_count,
        "resolved_count": resolved_count,
        "tied_count": tied_count,
        "tie_timeout_count": tie_timeout_count,
        "failed_count": len(errors),
        "processed_dispute_ids": processed_ids,
        "resolved_dispute_ids": resolved_ids,
        "tied_dispute_ids": tied_ids,
        "errors": errors,
    }


def _dispute_judge_loop(stop_event: threading.Event) -> None:
    _set_dispute_judge_state(running=True, started_at=_utc_now_iso())
    while not stop_event.wait(_DISPUTE_JUDGE_INTERVAL_SECONDS):
        started = _utc_now_iso()
        try:
            summary = _run_pending_dispute_judgments(actor_owner_id="system:dispute-judge")
            _set_dispute_judge_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
        except Exception as exc:
            _LOG.exception("Dispute judge loop failed.")
            _set_dispute_judge_state(
                last_run_at=started,
                last_error=str(exc),
            )
    _set_dispute_judge_state(running=False)


def _run_agent_health_checks() -> dict:
    """Check health endpoints of all external agents that have a healthcheck_url."""
    agents_to_check = registry.get_agents(include_internal=False)
    checked = 0
    healthy = 0
    unhealthy = 0
    for agent in agents_to_check:
        url = (agent.get("healthcheck_url") or "").strip()
        if not url:
            continue
        try:
            validated_url = _validate_outbound_url(url, "healthcheck_url")
            import httpx as _httpx
            resp = _httpx.get(validated_url, timeout=10, follow_redirects=True)
            status = "healthy" if 200 <= resp.status_code < 300 else "unhealthy"
        except Exception:
            status = "unhealthy"
        registry.update_agent_health(agent["agent_id"], status, _utc_now_iso())
        checked += 1
        if status == "healthy":
            healthy += 1
        else:
            unhealthy += 1
    return {"checked": checked, "healthy": healthy, "unhealthy": unhealthy}


def _agent_health_loop(stop_event: threading.Event) -> None:
    while not stop_event.wait(_AGENT_HEALTH_CHECK_INTERVAL_SECONDS):
        try:
            _run_agent_health_checks()
        except Exception:
            _LOG.exception("Agent health check loop failed.")


def _payments_reconciliation_loop(stop_event: threading.Event) -> None:
    _set_payments_reconciliation_state(running=True, started_at=_utc_now_iso())
    while not stop_event.is_set():
        started = _utc_now_iso()
        try:
            summary = payments.record_reconciliation_run(
                max_mismatches=_PAYMENTS_RECONCILIATION_MAX_MISMATCHES
            )
            _set_payments_reconciliation_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
            if not bool(summary.get("invariant_ok")):
                logging_utils.log_event(
                    _LOG,
                    logging.ERROR,
                    "payments.reconciliation_invariant_failed",
                    {
                        "run_id": summary.get("run_id"),
                        "drift_cents": summary.get("drift_cents"),
                        "mismatch_count": summary.get("mismatch_count"),
                    },
                )
        except Exception as exc:
            _LOG.exception("Payments reconciliation loop failed.")
            _set_payments_reconciliation_state(
                last_run_at=started,
                last_error=str(exc),
            )
        if stop_event.wait(_PAYMENTS_RECONCILIATION_INTERVAL_SECONDS):
            break
    _set_payments_reconciliation_state(running=False)


def _enqueue_job_event_hook_deliveries(event: dict) -> None:
    owner_ids = {event.get("caller_owner_id"), event.get("agent_owner_id")}
    owner_ids = {owner_id for owner_id in owner_ids if owner_id}
    if not owner_ids:
        return

    placeholders = ",".join(["?"] * len(owner_ids))
    payload_json = _stable_json_text(event)
    now = _utc_now_iso()
    with jobs._conn() as conn:
        hooks = conn.execute(
            f"""
            SELECT * FROM job_event_hooks
            WHERE is_active = 1 AND owner_id IN ({placeholders})
            """,
            tuple(owner_ids),
        ).fetchall()

    if not hooks:
        return

    for row in hooks:
        hook = _hook_row_to_dict(row)
        with jobs._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO job_event_deliveries
                    (event_id, hook_id, owner_id, target_url, secret, payload,
                     status, attempt_count, next_attempt_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    hook["hook_id"],
                    hook["owner_id"],
                    hook["target_url"],
                    hook.get("secret"),
                    payload_json,
                    now,
                    now,
                    now,
                ),
            )


_JOB_CALLBACK_HOOK_PREFIX = "callback:"


def _enqueue_job_callback(job: dict, event_id: int) -> None:
    """Enqueue a one-time push delivery to job.callback_url on terminal state."""
    callback_url = (job.get("callback_url") or "").strip()
    if not callback_url:
        return
    try:
        safe_url = _validate_hook_url(callback_url)
    except ValueError:
        return

    hook_id = f"{_JOB_CALLBACK_HOOK_PREFIX}{job['job_id']}"
    payload = {
        "job_id": job["job_id"],
        "agent_id": job.get("agent_id"),
        "status": job.get("status"),
        "output_payload": job.get("output_payload"),
        "error_message": job.get("error_message"),
        "completed_at": job.get("completed_at"),
        "settled_at": job.get("settled_at"),
        "price_cents": job.get("price_cents"),
    }
    now = _utc_now_iso()
    with jobs._conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO job_event_deliveries
                (event_id, hook_id, owner_id, target_url, secret, payload,
                 status, attempt_count, next_attempt_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (
                event_id,
                hook_id,
                job.get("caller_owner_id", ""),
                safe_url,
                (job.get("callback_secret") or "").strip() or None,
                json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str),
                now,
                now,
                now,
            ),
        )


def _hook_backoff_seconds(attempt_count: int) -> int:
    exponent = max(0, attempt_count - 1)
    delay = _HOOK_DELIVERY_BASE_DELAY_SECONDS * (2 ** exponent)
    return min(delay, _HOOK_DELIVERY_MAX_DELAY_SECONDS)


def _claim_due_hook_delivery(now_iso: str) -> dict | None:
    with jobs._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT *
            FROM job_event_deliveries
            WHERE status = 'pending'
              AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC, delivery_id ASC
            LIMIT 1
            """,
            (now_iso,),
        ).fetchone()
        if row is None:
            return None

        claim_until_iso = (
            datetime.fromisoformat(now_iso) + timedelta(seconds=_HOOK_DELIVERY_CLAIM_LEASE_SECONDS)
        ).isoformat()
        result = conn.execute(
            """
            UPDATE job_event_deliveries
            SET next_attempt_at = ?,
                last_attempt_at = ?,
                updated_at = ?
            WHERE delivery_id = ?
              AND status = 'pending'
              AND next_attempt_at <= ?
            """,
            (claim_until_iso, now_iso, now_iso, row["delivery_id"], now_iso),
        )
        if result.rowcount == 0:
            return None

        claimed = conn.execute(
            "SELECT * FROM job_event_deliveries WHERE delivery_id = ?",
            (row["delivery_id"],),
        ).fetchone()
    return dict(claimed) if claimed else None


def _update_hook_attempt_metadata(
    hook_id: str,
    attempted_at: str,
    success: bool,
    status_code: int | None,
    error_text: str | None,
) -> None:
    with jobs._conn() as conn:
        conn.execute(
            """
            UPDATE job_event_hooks
            SET last_attempt_at = ?,
                last_success_at = CASE WHEN ? = 1 THEN ? ELSE last_success_at END,
                last_status_code = ?,
                last_error = ?
            WHERE hook_id = ?
            """,
            (
                attempted_at,
                1 if success else 0,
                attempted_at,
                status_code,
                error_text,
                hook_id,
            ),
        )


def _mark_hook_delivery(
    delivery_id: int,
    *,
    status: str,
    next_attempt_at: str,
    attempt_count: int | None = None,
    status_code: int | None,
    error_text: str | None,
    now_iso: str,
    mark_success: bool,
) -> None:
    with jobs._conn() as conn:
        conn.execute(
            """
            UPDATE job_event_deliveries
            SET status = ?,
                next_attempt_at = ?,
                attempt_count = COALESCE(?, attempt_count),
                last_status_code = ?,
                last_error = ?,
                last_success_at = CASE WHEN ? = 1 THEN ? ELSE last_success_at END,
                updated_at = ?
            WHERE delivery_id = ?
            """,
            (
                status,
                next_attempt_at,
                attempt_count,
                status_code,
                error_text,
                1 if mark_success else 0,
                now_iso,
                now_iso,
                delivery_id,
            ),
        )


def _process_due_hook_deliveries(limit: int = _HOOK_DELIVERY_BATCH_SIZE) -> dict:
    batch_limit = min(max(1, int(limit)), 500)
    processed = 0
    delivered = 0
    retried = 0
    failed = 0
    cancelled = 0

    for _ in range(batch_limit):
        now_iso = _utc_now_iso()
        delivery = _claim_due_hook_delivery(now_iso)
        if delivery is None:
            break

        processed += 1
        delivery_id = int(delivery["delivery_id"])
        hook_id = str(delivery["hook_id"])
        attempt_count = int(delivery["attempt_count"])

        is_job_callback = hook_id.startswith(_JOB_CALLBACK_HOOK_PREFIX)
        if not is_job_callback:
            with jobs._conn() as conn:
                hook_row = conn.execute(
                    "SELECT is_active FROM job_event_hooks WHERE hook_id = ?",
                    (hook_id,),
                ).fetchone()

            if hook_row is None or int(hook_row["is_active"]) != 1:
                error_text = "Hook is inactive or deleted."
                _update_hook_attempt_metadata(
                    hook_id=hook_id,
                    attempted_at=now_iso,
                    success=False,
                    status_code=None,
                    error_text=error_text,
                )
                _mark_hook_delivery(
                    delivery_id,
                    status="cancelled",
                    next_attempt_at=now_iso,
                    attempt_count=attempt_count,
                    status_code=None,
                    error_text=error_text,
                    now_iso=now_iso,
                    mark_success=False,
                )
                cancelled += 1
                continue

        try:
            safe_target_url = _validate_hook_url(str(delivery["target_url"]))
        except ValueError as exc:
            error_text = f"Blocked unsafe hook target: {exc}"
            if not is_job_callback:
                _update_hook_attempt_metadata(
                    hook_id=hook_id,
                    attempted_at=now_iso,
                    success=False,
                    status_code=None,
                    error_text=error_text,
                )
            _mark_hook_delivery(
                delivery_id,
                status="failed" if (attempt_count + 1) >= _HOOK_DELIVERY_MAX_ATTEMPTS else "pending",
                next_attempt_at=(
                    now_iso
                    if (attempt_count + 1) >= _HOOK_DELIVERY_MAX_ATTEMPTS
                    else (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=_hook_backoff_seconds(attempt_count + 1))
                    ).isoformat()
                ),
                attempt_count=attempt_count + 1,
                status_code=None,
                error_text=error_text,
                now_iso=now_iso,
                mark_success=False,
            )
            if (attempt_count + 1) >= _HOOK_DELIVERY_MAX_ATTEMPTS:
                failed += 1
            else:
                retried += 1
            continue

        try:
            payload = json.loads(delivery["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload_bytes = _stable_json_text(payload).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "X-Aztea-Event-Id": str(delivery["event_id"]),
            "X-Aztea-Event-Type": str(payload.get("event_type") or "unknown"),
        }
        secret = (delivery.get("secret") or "").strip()
        if secret:
            digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
            headers["X-Aztea-Signature"] = f"sha256={digest}"

        status_code = None
        error_text = None
        success = False
        try:
            resp = http.post(
                safe_target_url,
                data=payload_bytes,
                headers=headers,
                timeout=5,
                allow_redirects=False,
            )
            status_code = int(resp.status_code)
            success = 200 <= status_code < 300
            if not success:
                error_text = f"Non-2xx status: {status_code}"
        except http.RequestException as exc:
            error_text = str(exc)

        if not is_job_callback:
            _update_hook_attempt_metadata(
                hook_id=hook_id,
                attempted_at=now_iso,
                success=success,
                status_code=status_code,
                error_text=error_text,
            )

        if success:
            _mark_hook_delivery(
                delivery_id,
                status="delivered",
                next_attempt_at=now_iso,
                attempt_count=attempt_count,
                status_code=status_code,
                error_text=None,
                now_iso=now_iso,
                mark_success=True,
            )
            delivered += 1
            continue

        next_attempt_count = attempt_count + 1
        if next_attempt_count >= _HOOK_DELIVERY_MAX_ATTEMPTS:
            _mark_hook_delivery(
                delivery_id,
                status="failed",
                next_attempt_at=now_iso,
                attempt_count=next_attempt_count,
                status_code=status_code,
                error_text=error_text,
                now_iso=now_iso,
                mark_success=False,
            )
            failed += 1
            continue

        retry_delay = _hook_backoff_seconds(next_attempt_count)
        next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_delay)).isoformat()
        _mark_hook_delivery(
            delivery_id,
            status="pending",
            next_attempt_at=next_attempt_at,
            attempt_count=next_attempt_count,
            status_code=status_code,
            error_text=error_text,
            now_iso=now_iso,
            mark_success=False,
        )
        retried += 1

    with jobs._conn() as conn:
        pending = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_event_deliveries
            WHERE status = 'pending'
            """
        ).fetchone()["count"]
        failed_total = conn.execute(
            "SELECT COUNT(*) AS count FROM job_event_deliveries WHERE status = 'failed'"
        ).fetchone()["count"]

    return {
        "processed": int(processed),
        "delivered": int(delivered),
        "retried": int(retried),
        "failed": int(failed),
        "cancelled": int(cancelled),
        "dead_lettered": int(failed),
        "pending": int(pending),
        "failed_total": int(failed_total),
        "dead_letter_total": int(failed_total),
    }


def _hook_delivery_loop(stop_event: threading.Event) -> None:
    _set_hook_worker_state(running=True, started_at=_utc_now_iso())
    while not stop_event.wait(_HOOK_DELIVERY_INTERVAL_SECONDS):
        started = _utc_now_iso()
        try:
            summary = _process_due_hook_deliveries(limit=_HOOK_DELIVERY_BATCH_SIZE)
            _set_hook_worker_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
        except Exception as exc:
            _LOG.exception("Hook delivery loop failed.")
            _set_hook_worker_state(
                last_run_at=started,
                last_error=str(exc),
            )
    _set_hook_worker_state(running=False)


def _list_hook_deliveries(
    owner_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    capped_limit = min(max(1, limit), 500)
    where: list[str] = []
    params: list[Any] = []
    if owner_id is not None:
        where.append("owner_id = ?")
        params.append(owner_id)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(capped_limit)
    with jobs._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM job_event_deliveries
            {where_sql}
            ORDER BY created_at DESC, delivery_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


