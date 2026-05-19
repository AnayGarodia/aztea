
from core import db as _db
# server.application shard 6 — background sweeper, jobs metrics, onboarding
# routes (agent.md spec, validate, ingest), and auth routes (register, login,
# me, legal accept, keys CRUD). First shard that registers HTTP routes.


def _sweep_jobs(
    retry_delay_seconds: int = _DEFAULT_RETRY_DELAY_SECONDS,
    sla_seconds: int = _DEFAULT_SLA_SECONDS,
    limit: int = 100,
    actor_owner_id: str = "system:sweeper",
) -> dict:
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds must be >= 0.")
    if sla_seconds <= 0:
        raise ValueError("sla_seconds must be > 0.")
    limit = min(max(1, limit), 500)

    expired = jobs.list_jobs_with_expired_leases(limit=limit)
    timeout_failed_job_ids: list[str] = []
    timeout_retry_job_ids: list[str] = []
    for item in expired:
        updated = jobs.mark_job_timeout(
            item["job_id"],
            retry_delay_seconds=retry_delay_seconds,
            allow_retry=True,
        )
        if updated is None:
            continue
        if updated.get("status") == "pending":
            timeout_retry_job_ids.append(updated["job_id"])
            try:
                _wake_builtin_worker()
            except Exception:
                _LOG.debug("Failed to wake builtin worker after timeout retry.", exc_info=True)
            _record_job_event(
                updated,
                "job.timeout_retry_scheduled",
                actor_owner_id=actor_owner_id,
                payload={
                    "retry_count": updated.get("retry_count"),
                    "next_retry_at": updated.get("next_retry_at"),
                },
            )
        else:
            settled = _settle_failed_job(
                updated,
                actor_owner_id=actor_owner_id,
                event_type="job.timeout_terminal",
            )
            timeout_failed_job_ids.append(settled["job_id"])

    clarification_timeout_failed_job_ids: list[str] = []
    clarification_timeout_proceeded_job_ids: list[str] = []
    expired_clarification = jobs.list_jobs_with_expired_clarification_deadline(
        limit=limit
    )
    for item in expired_clarification:
        timeout_policy = (
            str(item.get("clarification_timeout_policy") or "").strip().lower()
            or "fail"
        )
        if timeout_policy == "proceed":
            resumed = jobs.update_job_status(item["job_id"], "running", completed=False)
            if resumed is None:
                continue
            clarification_timeout_proceeded_job_ids.append(resumed["job_id"])
            _record_job_event(
                resumed,
                "job.clarification_timeout_proceeded",
                actor_owner_id=actor_owner_id,
                payload={
                    "clarification_deadline_at": item.get("clarification_deadline_at")
                },
            )
            continue

        failed = jobs.update_job_status(
            item["job_id"],
            "failed",
            error_message="Clarification response timeout reached.",
            completed=True,
        )
        if failed is None:
            continue
        settled = _settle_failed_job(
            failed,
            actor_owner_id=actor_owner_id,
            event_type="job.clarification_timeout_failed",
            refund_fraction=1.0,
        )
        clarification_timeout_failed_job_ids.append(settled["job_id"])

    sla_failed_job_ids: list[str] = []
    for item in jobs.list_jobs_past_sla(sla_seconds=sla_seconds, limit=limit):
        updated = jobs.update_job_status(
            item["job_id"],
            "failed",
            error_message="Job exceeded SLA and was automatically failed.",
            completed=True,
        )
        if updated is None:
            continue
        settled = _settle_failed_job(
            updated, actor_owner_id=actor_owner_id, event_type="job.sla_expired"
        )
        sla_failed_job_ids.append(settled["job_id"])

    # B15, 2026-05-19: auto-fail jobs that nobody ever claimed. The
    # pre-fix symptom was the 2026-05-19 power-user report's "agents
    # accepted hires, opened escrow, never executed" — caller couldn't
    # tell whether to wait or cancel. Sweeper now scans for pending
    # jobs past claim_deadline_at, marks them failed with
    # error_message=agent.no_workers_claimed, and triggers the standard
    # refund pipeline. The lookup is a thin SELECT; the actual transition
    # uses the existing update_job_status + _settle_failed_job rails so
    # the refund path is identical to other terminal-fail cases.
    claim_deadline_failed_job_ids: list[str] = []
    try:
        with jobs._conn() as _conn_b15:
            now_iso = _utc_now_iso()
            stranded = _conn_b15.execute(
                """
                SELECT job_id FROM jobs
                WHERE status = 'pending'
                  AND claim_deadline_at IS NOT NULL
                  AND claim_deadline_at < %s
                LIMIT %s
                """,
                (now_iso, limit),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 — sweeper must never break
        _LOG.warning("B15 claim-deadline scan failed: %s", exc, exc_info=True)
        stranded = []
    for row in stranded:
        stranded_id = row["job_id"] if isinstance(row, dict) else row[0]
        updated = jobs.update_job_status(
            stranded_id,
            "failed",
            error_message="agent.no_workers_claimed",
            completed=True,
        )
        if updated is None:
            continue
        settled = _settle_failed_job(
            updated,
            actor_owner_id=actor_owner_id,
            event_type="job.no_workers_claimed",
        )
        claim_deadline_failed_job_ids.append(settled["job_id"])
        # B15 follow-up, 2026-05-19: increment the per-agent counter so
        # oncall can alarm on a spike (signal of a misconfigured external
        # agent endpoint or a worker outage). Never raises; observability
        # must not block the sweeper.
        try:
            from core import observability as _obs_b15
            _obs_b15.job_no_workers_claimed_total.labels(
                agent_id=str(settled.get("agent_id") or "unknown"),
            ).inc()
        except Exception:  # noqa: BLE001 — observability is best-effort
            pass

    due_retry = jobs.list_jobs_due_for_retry(limit=limit)
    retry_ready_job_ids: list[str] = []
    for item in due_retry:
        previous_next_retry_at = item.get("next_retry_at")
        advanced = jobs.mark_retry_ready(item["job_id"])
        if advanced is None:
            continue
        retry_ready_job_ids.append(advanced["job_id"])
        try:
            _wake_builtin_worker()
        except Exception:
            _LOG.debug("Failed to wake builtin worker after retry ready.", exc_info=True)
        _record_job_event(
            advanced,
            "retry_ready",
            actor_owner_id=actor_owner_id,
            payload={"previous_next_retry_at": previous_next_retry_at},
        )
    output_verification_expired_job_ids: list[str] = []
    output_verification_auto_settled_job_ids: list[str] = []
    for item in jobs.list_jobs_with_expired_output_verification(limit=limit):
        expired = jobs.mark_output_verification_expired(item["job_id"])
        if expired is None:
            continue
        output_verification_expired_job_ids.append(expired["job_id"])
        _record_job_event(
            expired,
            "job.output_verification_expired",
            actor_owner_id=actor_owner_id,
            payload={
                "output_verification_deadline_at": item.get(
                    "output_verification_deadline_at"
                )
            },
        )
        auto_settled = _settle_successful_job(expired, actor_owner_id=actor_owner_id)
        if auto_settled.get("settled_at"):
            output_verification_auto_settled_job_ids.append(auto_settled["job_id"])
    completed_pending_settlement = jobs.list_completed_jobs_pending_settlement(
        limit=limit
    )
    settled_successful_job_ids: list[str] = []
    for item in completed_pending_settlement:
        settled = _settle_successful_job(item, actor_owner_id=actor_owner_id)
        if settled.get("settled_at"):
            settled_successful_job_ids.append(settled["job_id"])
    endpoint_health_summary = _monitor_agent_endpoints(limit=limit)
    suspension_summary = _auto_suspend_low_performing_agents(actor_owner_id)
    decay_summary = _apply_reputation_decay()
    latency_decay_summary = _apply_latency_decay()
    cache_evicted_count = _cache.evict_expired()
    probation_summary = _maybe_sweep_probation(actor_owner_id=actor_owner_id)
    # Hold-window expiration release: any wallet_holds row whose hold_until
    # has passed without a rating/dispute clawback is released cleanly so
    # the agent can withdraw the previously-reserved cents. Bounded per
    # tick to keep the write window short.
    try:
        from core.payments import holds as _wallet_holds
        from core import observability as _obs

        released_count = _wallet_holds.release_expired_holds(limit=limit)
        if released_count:
            _obs.wallet_hold_released_total.labels(
                reason=_wallet_holds.RELEASE_REASON_WINDOW_EXPIRED
            ).inc(released_count)
    except Exception:
        # Hold release failures must not block the rest of the sweep.
        # release_expired_holds() already logs and isolates per-row errors;
        # this catch only protects against import-time / table-missing
        # bootstrap failures during partial deploys.
        _LOG.exception("wallet_holds.release_expired_holds failed during sweep")
        released_count = 0
    return {
        "expired_leases_scanned": len(expired),
        "due_retry_count": len(due_retry),
        "retry_ready_count": len(retry_ready_job_ids),
        "retry_ready_job_ids": retry_ready_job_ids,
        "timeout_retry_job_ids": timeout_retry_job_ids,
        "timeout_failed_job_ids": timeout_failed_job_ids,
        "clarification_timeout_scanned": len(expired_clarification),
        "clarification_timeout_failed_job_ids": clarification_timeout_failed_job_ids,
        "clarification_timeout_proceeded_job_ids": clarification_timeout_proceeded_job_ids,
        "sla_failed_job_ids": sla_failed_job_ids,
        # B15, 2026-05-19
        "claim_deadline_failed_job_ids": claim_deadline_failed_job_ids,
        "output_verification_expired_job_ids": output_verification_expired_job_ids,
        "output_verification_auto_settled_job_ids": output_verification_auto_settled_job_ids,
        "completed_pending_settlement_scanned": len(completed_pending_settlement),
        "settled_successful_count": len(settled_successful_job_ids),
        "settled_successful_job_ids": settled_successful_job_ids,
        **endpoint_health_summary,
        "auto_suspended_count": int(suspension_summary["auto_suspended_count"]),
        "auto_suspended_agent_ids": suspension_summary["auto_suspended_agent_ids"],
        "reputation_decay": decay_summary,
        "latency_decay": latency_decay_summary,
        "cache_evicted_count": cache_evicted_count,
        "probation_graduated_count": probation_summary["count"],
        "probation_graduated_agent_ids": probation_summary["agent_ids"],
        "wallet_holds_released_count": released_count,
    }


# Probation graduation is much cheaper to skip than to run on every sweep
# tick; the job sweeper fires every ~2s and graduation only changes state
# at human timescales. Throttle to feature_flags.probation_sweep_interval.
_LAST_PROBATION_SWEEP_AT_MONO: float = 0.0


def _maybe_sweep_probation(*, actor_owner_id: str) -> dict[str, Any]:
    """Run the probation graduation pass at most once per configured interval.

    Returns ``{"count": int, "agent_ids": list[str]}``. On any failure the
    graduation function logs the exception and returns an empty list — a
    failed graduation must never break the broader job sweep.
    """
    global _LAST_PROBATION_SWEEP_AT_MONO
    now_mono = time.monotonic()
    interval = _feature_flags.probation_sweep_interval_seconds()
    if (now_mono - _LAST_PROBATION_SWEEP_AT_MONO) < interval:
        return {"count": 0, "agent_ids": []}
    _LAST_PROBATION_SWEEP_AT_MONO = now_mono

    # `actor_owner_id` is accepted for symmetry with the rest of the sweeper
    # surface but isn't used here: graduation lands an audit row in
    # `agents.review_note` + `reviewed_by='system'` already, and the
    # `job_events` table is keyed on `job_id` (graduation is agent-scoped,
    # not job-scoped).
    del actor_owner_id

    try:
        graduated = registry.graduate_probation_listings()
    except Exception:
        _LOG.exception("probation graduation sweep failed")
        return {"count": 0, "agent_ids": []}

    if graduated:
        _LOG.info(
            "probation_graduation",
            extra={"agent_ids": graduated, "count": len(graduated)},
        )
    return {"count": len(graduated), "agent_ids": graduated}


def _set_sweeper_state(**updates: Any) -> None:
    with _SWEEPER_STATE_LOCK:
        _SWEEPER_STATE.update(updates)


_PENDING_STARVATION_THRESHOLD_SECONDS = max(60, _SWEEPER_INTERVAL_SECONDS * 5)


def _emit_pending_starvation_signal() -> None:
    """Surface jobs that have sat in `pending` longer than the threshold.

    Audit 2026-05-16 #6: three batch jobs sat in `pending` for 14+ minutes
    while the worker pool reported 23/24 free slots. The bug went silent
    because nothing logged or counted the stuck rows. This emits a single
    structured warning per sweep pass partitioned by ``(agent_id,
    endpoint_kind)`` so operators can see which agent has no worker,
    independent of whether the underlying claim race is fixed.
    """
    cutoff_iso = (
        datetime.now(timezone.utc)
        - timedelta(seconds=_PENDING_STARVATION_THRESHOLD_SECONDS)
    ).isoformat()
    try:
        with jobs._conn() as conn:
            rows = conn.execute(
                """
                SELECT agent_id, COUNT(*) AS stuck_count
                FROM jobs
                WHERE status = 'pending' AND created_at < %s
                GROUP BY agent_id
                """,
                (cutoff_iso,),
            ).fetchall()
    except Exception:
        _LOG.exception("pending.starvation.query_failed")
        return
    for row in rows:
        agent_id = row["agent_id"] if "agent_id" in row.keys() else row[0]
        stuck_count = (
            int(row["stuck_count"]) if "stuck_count" in row.keys() else int(row[1])
        )
        if stuck_count <= 0:
            continue
        _LOG.warning(
            "pending.starvation agent_id=%s stuck_count=%d threshold_seconds=%d",
            agent_id,
            stuck_count,
            _PENDING_STARVATION_THRESHOLD_SECONDS,
        )


# How often the sweeper runs the auto-hire decision retention pass. 24h is
# fine because the rollup itself is idempotent and the raw retention window
# is 90 days — missing a tick by an hour costs nothing.
_DECISION_RETENTION_INTERVAL_SECONDS: int = 24 * 60 * 60
_decision_retention_last_run_at: float = 0.0


def _maybe_run_decision_retention() -> None:
    """Side-effect: invoke ``observability.run_decision_retention`` at most once per day.

    Why: the retention sweep is cheap when the table is small and bounded
    by a single index scan when large. Gating it inside the existing
    sweeper avoids spinning up a new background thread for one daily job.
    """
    global _decision_retention_last_run_at
    now = time.monotonic()
    if now - _decision_retention_last_run_at < _DECISION_RETENTION_INTERVAL_SECONDS:
        return
    _decision_retention_last_run_at = now
    summary = _observability.run_decision_retention()
    if summary.get("raw_rows_deleted"):
        logging_utils.log_event(
            _LOG, logging.INFO, "decision_retention.swept", summary,
        )


def _jobs_sweeper_loop(stop_event: threading.Event) -> None:
    _set_sweeper_state(running=True, started_at=_utc_now_iso())
    while not stop_event.wait(_SWEEPER_INTERVAL_SECONDS):
        started = _utc_now_iso()
        try:
            summary = _sweep_jobs(
                retry_delay_seconds=_SWEEPER_RETRY_DELAY_SECONDS,
                sla_seconds=_SWEEPER_SLA_SECONDS,
                limit=_SWEEPER_LIMIT,
                actor_owner_id="system:scheduler",
            )
            _emit_pending_starvation_signal()
            _maybe_run_decision_retention()
            _run_workspaces_sweeper_pass(summary)
            _run_sandbox_sweeper_pass(summary)
            _set_sweeper_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
            active = {k: v for k, v in summary.items() if isinstance(v, int) and v > 0}
            if active:
                logging_utils.log_event(
                    _LOG, logging.INFO, "sweeper.pass_completed", active
                )
        except Exception as exc:
            _LOG.exception("Jobs sweeper loop failed.")
            _set_sweeper_state(
                last_run_at=started,
                last_error=str(exc),
            )
    _set_sweeper_state(running=False)


def _run_workspaces_sweeper_pass(summary: dict[str, Any]) -> None:
    """Piggyback the workspaces sweeper on the jobs sweeper tick.

    Workspaces are not jobs, but they share the "background pass every
    N seconds" cadence and a separate thread would double the
    background-thread count for negligible benefit. Failures are logged
    but never propagate — the jobs sweeper must keep running even if a
    schema-mismatch leaves workspaces unsweepable on this tick.

    The counts land under ``summary["workspaces_*"]`` so
    ``sweeper.pass_completed`` logs them when non-zero.
    """
    try:
        from core import workspaces as _workspaces
        counts = _workspaces.run_sweeper()
        summary["workspaces_expired_marked"] = counts.get("expired_marked", 0)
        summary["workspaces_content_purged"] = counts.get("content_purged", 0)
    except Exception as exc:  # noqa: BLE001 — never break the jobs loop
        _LOG.warning("workspaces sweeper failed: %s", exc, exc_info=True)
        summary["workspaces_sweeper_error"] = str(exc)


def _run_sandbox_sweeper_pass(summary: dict[str, Any]) -> None:
    """Piggyback the live_sandbox lifetime sweeper on the jobs tick.

    Bug #1 from the 2026-05-18 live_sandbox audit: ``core.sandbox.sweeper``
    had the logic to expire sandboxes past ``expires_at`` but nothing ever
    called it — containers booted with ``lifetime.max_minutes=3`` lived
    forever. Wiring it into the existing jobs tick avoids spinning up a
    second background thread for one periodic job.

    Failures never propagate: the jobs loop must keep running even if
    Docker is missing on this host.
    """
    try:
        from core.sandbox import sweeper as _sandbox_sweeper
        counts = _sandbox_sweeper.sweep_once()
        summary["sandbox_expired_suspended"] = counts.get("expired_suspended", 0)
        summary["sandbox_idle_suspended"] = counts.get("idle_suspended", 0)
        summary["sandbox_auto_snapshot"] = counts.get("auto_snapshot", 0)
    except Exception as exc:  # noqa: BLE001 — never break the jobs loop
        _LOG.warning("sandbox sweeper failed: %s", exc, exc_info=True)
        summary["sandbox_sweeper_error"] = str(exc)


def _jobs_metrics(sla_seconds: int = _DEFAULT_SLA_SECONDS) -> dict:
    events_since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with jobs._conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
        ).fetchall()
        status_counts = {row["status"]: int(row["count"]) for row in rows}
        unsettled = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE settled_at IS NULL"
        ).fetchone()["count"]
        failed_unsettled = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE status = 'failed' AND settled_at IS NULL"
        ).fetchone()["count"]
        events_24h = conn.execute(
            "SELECT COUNT(*) AS count FROM job_events WHERE created_at >= %s",
            (events_since,),
        ).fetchone()["count"]
        delivery_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM job_event_deliveries
            GROUP BY status
            """
        ).fetchall()
        delivery_attempted_24h = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_event_deliveries
            WHERE last_attempt_at IS NOT NULL AND last_attempt_at >= %s
            """,
            (events_since,),
        ).fetchone()["count"]
        delivery_success_24h = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_event_deliveries
            WHERE last_success_at IS NOT NULL AND last_success_at >= %s
            """,
            (events_since,),
        ).fetchone()["count"]
        job_window_rows = conn.execute(
            """
            SELECT created_at, claimed_at, settled_at, timeout_count
            FROM jobs
            WHERE created_at >= %s
            """,
            (events_since,),
        ).fetchall()

    expired_leases_count = len(jobs.list_jobs_with_expired_leases(limit=200))
    due_retry_count = len(jobs.list_jobs_due_for_retry(limit=200))
    sla_breach_count = len(jobs.list_jobs_past_sla(sla_seconds=sla_seconds, limit=200))
    delivery_status_counts = {row["status"]: int(row["count"]) for row in delivery_rows}
    delivery_success_rate_24h = (
        round(float(delivery_success_24h) / float(delivery_attempted_24h), 4)
        if delivery_attempted_24h > 0
        else None
    )
    claim_latencies_ms: list[float] = []
    settlement_latencies_ms: list[float] = []
    timeout_jobs_24h = 0
    total_jobs_24h = len(job_window_rows)
    for row in job_window_rows:
        created_at = _parse_iso_datetime(row["created_at"])
        if created_at is None:
            continue

        claimed_at = _parse_iso_datetime(row["claimed_at"])
        if claimed_at is not None and claimed_at >= created_at:
            claim_latencies_ms.append(
                (claimed_at - created_at).total_seconds() * 1000.0
            )

        settled_at = _parse_iso_datetime(row["settled_at"])
        if settled_at is not None and settled_at >= created_at:
            settlement_latencies_ms.append(
                (settled_at - created_at).total_seconds() * 1000.0
            )

        if int(row["timeout_count"] or 0) > 0:
            timeout_jobs_24h += 1

    claim_p95_ms = (
        round(_p95(claim_latencies_ms) or 0.0, 3) if claim_latencies_ms else None
    )
    settlement_p95_ms = (
        round(_p95(settlement_latencies_ms) or 0.0, 3)
        if settlement_latencies_ms
        else None
    )
    timeout_rate_24h = (
        round(float(timeout_jobs_24h) / float(total_jobs_24h), 4)
        if total_jobs_24h > 0
        else None
    )
    slo = {
        "window_hours": 24,
        "targets": {
            "claim_latency_p95_ms_max": _SLO_CLAIM_P95_TARGET_MS,
            "settlement_latency_p95_ms_max": _SLO_SETTLEMENT_P95_TARGET_MS,
            "timeout_rate_max": _SLO_TIMEOUT_RATE_MAX,
            "hook_success_rate_min": _SLO_HOOK_SUCCESS_RATE_MIN,
        },
        "claim_latency_p95_ms": claim_p95_ms,
        "settlement_latency_p95_ms": settlement_p95_ms,
        "timeout_rate_last_24h": timeout_rate_24h,
        "hook_success_rate_last_24h": delivery_success_rate_24h,
    }

    alerts = []
    if failed_unsettled > 0:
        alerts.append(f"{failed_unsettled} failed jobs are not settled.")
    if expired_leases_count > 0:
        alerts.append(f"{expired_leases_count} jobs have expired worker leases.")
    if sla_breach_count > 0:
        alerts.append(f"{sla_breach_count} jobs breached SLA.")
    failed_deliveries = int(delivery_status_counts.get("failed", 0))
    if failed_deliveries > 0:
        alerts.append(f"{failed_deliveries} hook deliveries failed permanently.")
    if claim_p95_ms is not None and claim_p95_ms > _SLO_CLAIM_P95_TARGET_MS:
        alerts.append(
            f"Claim latency p95 {claim_p95_ms}ms exceeds SLO target {_SLO_CLAIM_P95_TARGET_MS}ms."
        )
    if (
        settlement_p95_ms is not None
        and settlement_p95_ms > _SLO_SETTLEMENT_P95_TARGET_MS
    ):
        alerts.append(
            "Settlement latency p95 "
            f"{settlement_p95_ms}ms exceeds SLO target {_SLO_SETTLEMENT_P95_TARGET_MS}ms."
        )
    if timeout_rate_24h is not None and timeout_rate_24h > _SLO_TIMEOUT_RATE_MAX:
        alerts.append(
            f"Timeout rate {timeout_rate_24h:.4f} exceeds SLO max {_SLO_TIMEOUT_RATE_MAX:.4f}."
        )
    if (
        delivery_success_rate_24h is not None
        and delivery_success_rate_24h < _SLO_HOOK_SUCCESS_RATE_MIN
    ):
        alerts.append(
            "Hook delivery success rate "
            f"{delivery_success_rate_24h:.4f} is below SLO min {_SLO_HOOK_SUCCESS_RATE_MIN:.4f}."
        )

    with _SWEEPER_STATE_LOCK:
        sweeper_state = dict(_SWEEPER_STATE)
    sweeper_last_summary = sweeper_state.get("last_summary")
    if not isinstance(sweeper_last_summary, dict):
        sweeper_last_summary = {}
    retry_ready_last_sweep = int(sweeper_last_summary.get("retry_ready_count") or 0)
    auto_suspended_last_sweep = int(
        sweeper_last_summary.get("auto_suspended_count") or 0
    )
    with _HOOK_WORKER_STATE_LOCK:
        hook_worker_state = dict(_HOOK_WORKER_STATE)
    with _BUILTIN_WORKER_STATE_LOCK:
        builtin_worker_state = dict(_BUILTIN_WORKER_STATE)
    with _DISPUTE_JUDGE_STATE_LOCK:
        dispute_judge_state = dict(_DISPUTE_JUDGE_STATE)
    with _PAYMENTS_RECONCILIATION_STATE_LOCK:
        payments_reconciliation_state = dict(_PAYMENTS_RECONCILIATION_STATE)

    return {
        "status_counts": status_counts,
        "unsettled_jobs": int(unsettled),
        "failed_unsettled_jobs": int(failed_unsettled),
        "expired_leases": expired_leases_count,
        "due_retries": due_retry_count,
        "retry_ready_last_sweep": retry_ready_last_sweep,
        "auto_suspended_last_sweep": auto_suspended_last_sweep,
        "sla_breaches": sla_breach_count,
        "events_last_24h": int(events_24h),
        "alerts": alerts,
        "sweeper": sweeper_state,
        "hook_worker": hook_worker_state,
        "builtin_worker": builtin_worker_state,
        "dispute_judge": dispute_judge_state,
        "payments_reconciliation": payments_reconciliation_state,
        "hook_delivery": {
            "status_counts": delivery_status_counts,
            "attempted_last_24h": int(delivery_attempted_24h),
            "delivered_last_24h": int(delivery_success_24h),
            "success_rate_last_24h": delivery_success_rate_24h,
        },
        "slo": slo,
    }


def _load_manifest_content(
    manifest_content: str | None, manifest_url: str | None
) -> tuple[str, str]:
    content = (manifest_content or "").strip()
    url = (manifest_url or "").strip()
    if bool(content) == bool(url):
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of manifest_content or manifest_url.",
        )
    if content:
        return content, "inline manifest"

    try:
        safe_url = _validate_outbound_url(url, "manifest_url")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        resp = http.get(safe_url, timeout=15, allow_redirects=False)
        if 300 <= int(resp.status_code) < 400:
            raise HTTPException(
                status_code=502, detail="manifest_url redirects are not allowed."
            )
        resp.raise_for_status()
    except http.RequestException as exc:
        _LOG.warning("Failed to fetch manifest_url %s: %s", safe_url, exc)
        raise HTTPException(
            status_code=502,
            detail=error_codes.make_error(
                error_codes.REGISTRY_MANIFEST_UNREACHABLE,
                f"Could not reach {safe_url}. Confirm the URL is publicly fetchable and try again.",
                {
                    "url": safe_url,
                    "timeout_seconds": 15,
                    "underlying": type(exc).__name__,
                },
            ),
        )
    if len(resp.content) > _MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Manifest too large (max {_MAX_BODY_BYTES // 1024} KB).",
        )
    text = resp.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Fetched manifest is empty.")
    return text, safe_url


def _sorted_agents(agents: list[dict], rank_by: str | None = None) -> list[dict]:
    if rank_by is None:
        mode = "trust"
    else:
        mode = rank_by.strip().lower()
    if mode == "trust":
        return sorted(
            agents,
            key=lambda a: (
                float(a.get("trust_score") or 0.0),
                float(a.get("confidence_score") or 0.0),
                int(a.get("total_calls") or 0),
            ),
            reverse=True,
        )
    if mode == "latency":
        return sorted(agents, key=lambda a: float(a.get("avg_latency_ms") or 0.0))
    if mode == "price":
        return sorted(agents, key=lambda a: float(a.get("price_per_call_usd") or 0.0))
    raise HTTPException(
        status_code=422, detail="rank_by must be one of: trust, latency, price."
    )


@app.get(
    "/agent.md",
    response_model=str,
    responses={
        200: {"content": {"text/markdown": {"schema": {"type": "string"}}}},
        **_error_responses(404, 429, 500),
    },
)
def onboarding_manifest_spec() -> Response:
    spec_path = os.path.join(_REPO_ROOT, "agent.md")
    if not os.path.exists(spec_path):
        raise HTTPException(status_code=404, detail="agent.md spec not found.")
    with open(spec_path, encoding="utf-8") as f:
        content = f.read()
    return Response(content=content, media_type="text/markdown")


@app.get(
    "/onboarding/spec",
    response_model=str,
    responses={
        200: {"content": {"text/markdown": {"schema": {"type": "string"}}}},
        **_error_responses(404, 429, 500),
    },
)
def onboarding_spec_alias() -> Response:
    return onboarding_manifest_spec()


@app.post(
    "/onboarding/validate",
    response_model=core_models.ManifestValidationResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("20/minute")
def onboarding_validate(
    request: Request,
    body: OnboardingValidateRequest,
    _: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.ManifestValidationResponse:
    manifest_content, source = _load_manifest_content(
        body.manifest_content, body.manifest_url
    )
    try:
        validated = onboarding.validate_manifest_content(
            manifest_content, source=source
        )
    except onboarding.ManifestValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return JSONResponse(content=validated)


@app.post(
    "/onboarding/ingest",
    status_code=201,
    response_model=core_models.OnboardingIngestResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
)
@limiter.limit("10/minute")
def onboarding_ingest(
    request: Request,
    body: OnboardingValidateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.OnboardingIngestResponse:
    _require_scope(caller, "worker")
    if caller["type"] != "master":
        _MAX_AGENTS_PER_OWNER = 20
        current_count = registry.count_owner_agents(caller["owner_id"])
        if current_count >= _MAX_AGENTS_PER_OWNER:
            raise HTTPException(
                status_code=403,
                detail=error_codes.make_error(
                    error_codes.REGISTRY_AGENT_LIMIT,
                    f"You've reached the {_MAX_AGENTS_PER_OWNER}-agent limit. "
                    "Delete or archive an existing agent to register a new one.",
                    {"current": current_count, "max": _MAX_AGENTS_PER_OWNER},
                ),
            )
    manifest_content, source = _load_manifest_content(
        body.manifest_content, body.manifest_url
    )
    try:
        payload = onboarding.build_registration_payload_from_manifest(
            manifest_content, source=source
        )
        safe_endpoint_url = _validate_agent_endpoint_url(
            request, payload["endpoint_url"]
        )
        safe_healthcheck_url = None
        if payload.get("healthcheck_url"):
            safe_healthcheck_url = _validate_outbound_url(
                payload["healthcheck_url"], "healthcheck_url"
            )
        safe_verifier_url = None
        if payload.get("output_verifier_url"):
            safe_verifier_url = _validate_outbound_url(
                payload["output_verifier_url"], "output_verifier_url"
            )
        endpoint_findings = _listing_safety.scan_agent_md_endpoint(safe_endpoint_url)
        if _listing_safety.has_block(endpoint_findings):
            block = next(
                f for f in endpoint_findings
                if f.level == _listing_safety.LEVEL_BLOCK
            )
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    "listing.safety_block", block.message,
                    {"code": block.code, "detail": block.detail},
                ),
            )
        _run_listing_safety_probe(
            safe_endpoint_url,
            input_schema=payload.get("input_schema"),
            output_schema=payload.get("output_schema"),
        )
        initial_review_status = (
            "probation" if caller["type"] != "master" else None
        )
        agent_id = registry.register_agent(
            name=payload["name"],
            description=payload["description"],
            endpoint_url=safe_endpoint_url,
            healthcheck_url=safe_healthcheck_url,
            price_per_call_usd=payload["price_per_call_usd"],
            tags=payload["tags"],
            input_schema=payload["input_schema"],
            output_schema=payload.get("output_schema"),
            output_verifier_url=safe_verifier_url,
            review_status=initial_review_status,
            owner_id=caller["owner_id"],
        )
    except onboarding.ManifestValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except (ValueError, _db.IntegrityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    agent = registry.get_agent_with_reputation(
        agent_id, include_unapproved=True
    ) or registry.get_agent(
        agent_id,
        include_unapproved=True,
    )
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "source": source,
            "registration_payload": payload,
            "agent": _agent_response(agent, caller),
            "message": "Manifest validated and agent registered.",
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# Auth routes  (public — no key required)
# ---------------------------------------------------------------------------


def _credit_starter_balance(result: dict[str, Any]) -> None:
    """Caller-capable users get platform-funded starter credit; builders do not."""
    role = result.get("role", "both")
    try:
        if role not in {"hirer", "both"} or payments.SIGNUP_CREDIT_CENTS <= 0:
            return
        wallet = payments.get_or_create_wallet(f"user:{result['user_id']}")
        credit_cents = int(payments.SIGNUP_CREDIT_CENTS)
        credit_fmt = f"${credit_cents / 100:.2f}"
        payments.deposit(
            wallet["wallet_id"],
            credit_cents,
            f"Signup credit ({credit_fmt} — platform-funded)",
        )
    except Exception:
        _LOG.warning(
            "Failed to credit starter balance for new user %s", result.get("user_id")
        )


def _auth_legal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "legal_acceptance_required": bool(
            payload.get("legal_acceptance_required", True)
        ),
        "legal_accepted_at": payload.get("legal_accepted_at"),
        "terms_version_current": str(
            payload.get("terms_version_current") or _auth.LEGAL_TERMS_VERSION
        ),
        "privacy_version_current": str(
            payload.get("privacy_version_current") or _auth.LEGAL_PRIVACY_VERSION
        ),
        "terms_version_accepted": payload.get("terms_version_accepted"),
        "privacy_version_accepted": payload.get("privacy_version_accepted"),
    }


@app.post(
    "/auth/register",
    status_code=201,
    response_model=core_models.AuthRegisterResponse,
    responses=_error_responses(400, 429, 500, 503),
)
@limiter.limit(_AUTH_RATE_LIMIT, key_func=get_remote_address)
def auth_register(
    request: Request, body: UserRegisterRequest
) -> core_models.AuthRegisterResponse:
    """Create a new user account. Returns the initial API key (shown once)."""
    role = body.role or "both"
    try:
        _auth.init_auth_db()
    except _db.OperationalError:
        _LOG.exception("Auth DB init failed on register.")
        raise HTTPException(
            status_code=503,
            detail="Authentication service is temporarily unavailable. Please try again.",
        )
    # register_user_result composes validation + DB with and_then — no try/except needed
    # for expected errors (bad input, duplicate email).  Unexpected DB errors propagate as 500.
    reg = _auth.register_user_result(body.username, body.email, body.password, role=role)
    if not reg:
        raise HTTPException(status_code=400, detail=reg.error)
    result = reg.value
    _credit_starter_balance(result)
    _email.send_welcome(result.get("email", ""), result.get("username", "there"), role=role)
    return JSONResponse(
        content={**result, **_auth_legal_payload(result)}, status_code=201
    )


@app.post(
    "/auth/login",
    response_model=core_models.AuthLoginResponse,
    responses=_error_responses(401, 429, 500, 503),
)
@limiter.limit(_AUTH_RATE_LIMIT, key_func=get_remote_address)
def auth_login(
    request: Request, body: UserLoginRequest
) -> core_models.AuthLoginResponse:
    """Verify credentials and return the user's session API key.

    By default the existing active session key is reused (raw value is None
    when reused — the client should already have it). Pass ``rotate=true`` in
    the body to force a fresh key.
    """
    login_kwargs = {
        "email": body.email,
        "password": body.password,
        "username": body.username,
        "rotate": body.rotate,
    }
    try:
        _auth.init_auth_db()
        result = _auth.login_user(**login_kwargs)
    except _auth.AccountSuspendedError:
        raise HTTPException(
            status_code=403,
            detail="This account has been suspended. Please contact support if you believe this is an error.",
        )
    except _db.OperationalError:
        _LOG.exception("Auth login failed; retrying after auth schema init.")
        try:
            _auth.init_auth_db()
            result = _auth.login_user(**login_kwargs)
        except _auth.AccountSuspendedError:
            raise HTTPException(
                status_code=403,
                detail="This account has been suspended. Please contact support if you believe this is an error.",
            )
        except _db.OperationalError:
            _LOG.exception("Auth login failed due to auth DB error.")
            raise HTTPException(
                status_code=503,
                detail="Authentication service is temporarily unavailable. Please try again.",
            )
    if result is None:
        raise HTTPException(
            status_code=401, detail="Invalid email/username or password."
        )
    return JSONResponse(content={**result, **_auth_legal_payload(result)})


@app.post(
    "/auth/google",
    response_model=core_models.AuthLoginResponse,
    responses=_error_responses(400, 401, 429, 500, 503),
)
@limiter.limit(_AUTH_RATE_LIMIT, key_func=get_remote_address)
def auth_google(
    request: Request, body: GoogleAuthRequest
) -> core_models.AuthLoginResponse:
    """Verify a Google ID token and log in (or create) the matching account.

    Verification uses Google's public ``tokeninfo`` endpoint so we don't need
    a heavyweight OAuth library. Requires ``GOOGLE_CLIENT_ID`` to be set on
    the server; if it isn't, the route returns 503 and the frontend hides the
    Google button.
    """
    client_id = (os.environ.get("GOOGLE_CLIENT_ID", "") or "").strip()
    if not client_id:
        raise HTTPException(
            status_code=503,
            detail="Google sign-in is not configured on this server.",
        )
    try:
        resp = http.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": body.id_token},
            timeout=5.0,
        )
    except Exception:
        _LOG.exception("Google tokeninfo request failed")
        raise HTTPException(
            status_code=503,
            detail="Could not reach Google to verify sign-in. Try again.",
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Google sign-in failed. Try again.")
    try:
        claims = resp.json()
    except ValueError:
        raise HTTPException(
            status_code=401, detail="Google sign-in returned an invalid response."
        )
    if claims.get("aud") != client_id:
        raise HTTPException(
            status_code=401, detail="Google sign-in token is for a different app."
        )
    if str(claims.get("email_verified")).lower() != "true":
        raise HTTPException(
            status_code=401, detail="Your Google account email is not verified."
        )
    email = (claims.get("email") or "").strip().lower()
    name = (claims.get("name") or "").strip()
    if not email:
        raise HTTPException(
            status_code=400, detail="Google sign-in did not return an email address."
        )
    try:
        _auth.init_auth_db()
        result, created = _auth.login_or_register_via_google(email, name)
    except _auth.AccountSuspendedError:
        raise HTTPException(
            status_code=403,
            detail="This account has been suspended. Please contact support if you believe this is an error.",
        )
    except _db.OperationalError:
        _LOG.exception("Google sign-in failed due to auth DB error.")
        raise HTTPException(
            status_code=503,
            detail="Authentication service is temporarily unavailable. Please try again.",
        )
    if created:
        _credit_starter_balance(result)
        try:
            _email.send_welcome(
                result.get("email", ""),
                result.get("username", "there"),
                role=result.get("role", "both"),
            )
        except Exception:  # pragma: no cover - welcome email is non-fatal
            _LOG.exception("Welcome email failed for Google signup")
    return JSONResponse(content={**result, **_auth_legal_payload(result)})


@app.get(
    "/auth/me",
    response_model=core_models.AuthMeResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def auth_me(
    request: Request, caller: core_models.CallerContext = Depends(_require_api_key)
) -> core_models.AuthMeResponse:
    """Return the authenticated user's profile."""
    if caller["type"] == "master":
        return JSONResponse(
            content={
                "type": "master",
                "user_id": None,
                "username": "admin",
                "scopes": ["caller", "worker", "admin"],
            }
        )
    if caller["type"] == "agent_key":
        raise HTTPException(
            status_code=403, detail="Agent-scoped keys cannot access /auth/me."
        )
    user = caller["user"]
    return JSONResponse(
        content={
            "user_id": user["user_id"],
            "username": user["username"],
            "email": user["email"],
            "full_name": user.get("full_name"),
            "phone": user.get("phone"),
            "role": user.get("role") or "both",
            "scopes": caller.get("scopes") or [],
            **_auth_legal_payload(user),
        }
    )


# B23, 2026-05-19: /users/me — sister to /wallet (mutable) and an alias
# for /auth/me. GET returns the same profile as /auth/me; POST updates
# editable fields (full_name, phone). Email changes require a separate
# verification flow and are intentionally rejected here.
@app.get(
    "/users/me",
    response_model=core_models.AuthMeResponse,
    responses=_error_responses(401, 403, 429, 500),
    include_in_schema=True,
    summary="Alias for /auth/me — same profile, same status codes.",
)
@limiter.limit("60/minute")
def users_me_get(
    request: Request, caller: core_models.CallerContext = Depends(_require_api_key)
):
    return auth_me(request, caller)


@app.post(
    "/users/me",
    responses=_error_responses(400, 401, 403, 422, 429, 500),
    include_in_schema=True,
    summary="Update editable profile fields (full_name, phone).",
)
@limiter.limit("20/minute")
def users_me_update(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """B23: minimal profile-update surface. Accepts full_name and/or phone.

    Email changes require a verification flow and are intentionally
    rejected with 422 + a structured pointer at the future endpoint.
    Master keys and agent-scoped keys cannot update profiles — they
    don't have one (master) or shouldn't (agent_key, mirrors /auth/me's
    403).
    """
    if caller["type"] in {"master", "agent_key"}:
        raise HTTPException(
            status_code=403,
            detail="Not available for master or agent-scoped keys.",
        )
    allowed = {"full_name", "phone"}
    rejected = sorted(set(body.keys()) - allowed)
    if "email" in rejected:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                "Email changes require verification and are not yet supported.",
                {"field": "email", "next_step": "Contact support."},
            ),
        )
    if rejected:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                f"Unsupported field(s): {', '.join(rejected)}.",
                {"allowed_fields": sorted(allowed)},
            ),
        )
    user_id = caller["user"]["user_id"]
    try:
        updated = users.update_user_profile(
            user_id,
            full_name=body.get("full_name"),
            phone=body.get("phone"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return JSONResponse(
        content={
            "user_id": updated["user_id"],
            "username": updated["username"],
            "email": updated.get("email"),
            "full_name": updated.get("full_name"),
            "phone": updated.get("phone"),
            "role": updated.get("role"),
        }
    )


@app.patch(
    "/auth/role",
    responses=_error_responses(400, 401, 403, 429),
)
@limiter.limit("20/minute")
def auth_update_role(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    """Switch the authenticated user's role between builder, hirer, and both."""
    if caller["type"] != "user":
        raise HTTPException(
            status_code=403, detail="Not available for master or agent-scoped keys."
        )
    new_role = str((body or {}).get("role") or "").strip()
    if new_role not in {"builder", "hirer", "both"}:
        raise HTTPException(
            status_code=400, detail="role must be 'builder', 'hirer', or 'both'."
        )
    user_id = caller["user"]["user_id"]
    try:
        _auth.update_user_role(user_id, new_role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"role": new_role}


@app.patch(
    "/auth/me",
    responses=_error_responses(400, 401, 403, 429),
)
@limiter.limit("20/minute")
def auth_update_profile(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    """Edit the authenticated user's profile (username, full name, phone, email)."""
    if caller["type"] != "user":
        raise HTTPException(
            status_code=403, detail="Not available for master or agent-scoped keys."
        )
    payload = body or {}
    fields = {}
    for key in ("username", "full_name", "phone", "email"):
        if key in payload:
            fields[key] = payload[key]
    if not fields:
        raise HTTPException(status_code=400, detail="No editable fields supplied.")
    user_id = caller["user"]["user_id"]
    try:
        updated = _auth.update_profile(user_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(
        content={
            "user_id": updated["user_id"],
            "username": updated["username"],
            "email": updated["email"],
            "full_name": updated.get("full_name"),
            "phone": updated.get("phone"),
            "role": updated.get("role") or "both",
            "scopes": caller.get("scopes") or [],
            **_auth_legal_payload(updated),
        }
    )


@app.post(
    "/auth/change-password",
    responses=_error_responses(400, 401, 403, 429),
)
@limiter.limit("10/minute")
def auth_change_password(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    """Replace the user's password after verifying the current one.

    Existing API keys are revoked so every active session must re-authenticate.
    """
    if caller["type"] != "user":
        raise HTTPException(
            status_code=403, detail="Not available for master or agent-scoped keys."
        )
    payload = body or {}
    current = str(payload.get("current_password") or "")
    new_password = str(payload.get("new_password") or "")
    user_id = caller["user"]["user_id"]
    try:
        _auth.change_password(user_id, current, new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post(
    "/auth/legal/accept",
    response_model=core_models.AuthLegalAcceptResponse,
    responses=_error_responses(400, 401, 403, 429, 500),
)
@limiter.limit(_AUTH_RATE_LIMIT, key_func=get_remote_address)
def auth_accept_legal(
    request: Request,
    body: AuthLegalAcceptRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AuthLegalAcceptResponse:
    if caller["type"] != "user":
        raise HTTPException(
            status_code=403, detail="Not available for master or agent-scoped keys."
        )
    client_ip = _request_client_ip(request)
    accepted_ip = str(client_ip) if client_ip is not None else None
    try:
        result = _auth.accept_legal_terms(
            caller["user"]["user_id"],
            terms_version=body.terms_version,
            privacy_version=body.privacy_version,
            accepted_ip=accepted_ip,
            accepted_user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.LEGAL_VERSION_MISMATCH,
                str(exc),
                {
                    "terms_version_current": _auth.LEGAL_TERMS_VERSION,
                    "privacy_version_current": _auth.LEGAL_PRIVACY_VERSION,
                },
            ),
        )
    return JSONResponse(content={**result, **_auth_legal_payload(result)})


@app.get(
    "/auth/keys",
    response_model=core_models.ApiKeyListResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def auth_list_keys(
    request: Request, caller: core_models.CallerContext = Depends(_require_api_key)
) -> core_models.ApiKeyListResponse:
    """List the caller's API keys (metadata only; raw keys are never returned after creation)."""
    if caller["type"] != "user":
        raise HTTPException(
            status_code=403,
            detail=(
                "Master key is for ops only and cannot manage scoped sub-keys. "
                "Register a user account (`aztea register` or "
                "`POST /auth/register`), then use that account's API key to "
                "list / mint / rotate / revoke caller- or worker-scoped keys."
            ),
        )
    keys = _auth.list_api_keys(caller["user"]["user_id"])
    return JSONResponse(content={"keys": keys})


@app.post(
    "/auth/keys",
    status_code=201,
    response_model=core_models.ApiKeyCreateResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
)
@limiter.limit("10/minute")
def auth_create_key(
    request: Request,
    body: CreateKeyRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.ApiKeyCreateResponse:
    """Create a new named API key for the authenticated user."""
    if caller["type"] != "user":
        raise HTTPException(
            status_code=403,
            detail=(
                "Master key is for ops only and cannot manage scoped sub-keys. "
                "Register a user account (`aztea register` or "
                "`POST /auth/register`), then use that account's API key to "
                "list / mint / rotate / revoke caller- or worker-scoped keys."
            ),
        )
    requested_scopes = {str(scope).strip().lower() for scope in body.scopes}
    if "caller" in requested_scopes and body.per_job_cap_cents is None:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.VALIDATION_ERROR,
                "caller-scoped keys require per_job_cap_cents.",
                {"field": "per_job_cap_cents", "required_for_scope": "caller"},
            ),
        )
    try:
        result = _auth.create_api_key(
            caller["user"]["user_id"],
            body.name,
            scopes=body.scopes,
            max_spend_cents=body.max_spend_cents,
            per_job_cap_cents=body.per_job_cap_cents,
        )
    except _auth.KeyLimitExceededError as exc:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.AUTH_KEY_LIMIT,
                str(exc),
                {"max": _auth._MAX_KEYS_PER_USER},
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content=result, status_code=201)


@app.post(
    "/auth/keys/{key_id}/rotate",
    status_code=201,
    response_model=core_models.ApiKeyRotateResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("10/minute")
def auth_rotate_key(
    request: Request,
    key_id: str,
    body: RotateKeyRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.ApiKeyRotateResponse:
    if caller["type"] != "user":
        raise HTTPException(
            status_code=403,
            detail=(
                "Master key is for ops only and cannot manage scoped sub-keys. "
                "Register a user account (`aztea register` or "
                "`POST /auth/register`), then use that account's API key to "
                "list / mint / rotate / revoke caller- or worker-scoped keys."
            ),
        )
    try:
        result = _auth.rotate_api_key(
            key_id=key_id,
            user_id=caller["user"]["user_id"],
            name=body.name,
            scopes=body.scopes,
            max_spend_cents=body.max_spend_cents,
            per_job_cap_cents=body.per_job_cap_cents,
            max_spend_cents_provided="max_spend_cents" in body.model_fields_set,
            per_job_cap_cents_provided="per_job_cap_cents" in body.model_fields_set,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="Key not found or already revoked.")
    return JSONResponse(content=result, status_code=201)


@app.delete(
    "/auth/keys/{key_id}",
    status_code=200,
    response_model=core_models.ApiKeyRevokeResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("10/minute")
def auth_revoke_key(
    request: Request,
    key_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.ApiKeyRevokeResponse:
    """Revoke an API key by ID."""
    if caller["type"] != "user":
        raise HTTPException(
            status_code=403,
            detail=(
                "Master key is for ops only and cannot manage scoped sub-keys. "
                "Register a user account (`aztea register` or "
                "`POST /auth/register`), then use that account's API key to "
                "list / mint / rotate / revoke caller- or worker-scoped keys."
            ),
        )
    ok = _auth.revoke_api_key(key_id, caller["user"]["user_id"])
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found or already revoked.")
    return JSONResponse(content={"revoked": True})


@app.post(
    "/auth/signup/start",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 429, 500),
)
@limiter.limit(_AUTH_RATE_LIMIT, key_func=get_remote_address)
def auth_signup_start(request: Request, body: UserRegisterRequest) -> JSONResponse:
    """Begin email-verified signup. Sends a 6-digit OTP and stores the pending
    registration; the account is only created when /auth/signup/verify succeeds."""
    email = body.email.strip().lower()
    try:
        _auth.init_auth_db()
        otp = _auth.issue_signup_verification(
            body.username, body.email, body.password, role=body.role or "both"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _email.send_signup_verification_otp(email, otp)
    return JSONResponse(content={"sent": True, "email": email})


@app.post(
    "/auth/signup/resend",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 429, 500),
)
@limiter.limit("3/minute", key_func=get_remote_address)
def auth_signup_resend(request: Request, body: dict) -> JSONResponse:
    """Resend the verification OTP for an in-progress signup."""
    email = str(body.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required.")
    otp = _auth.reissue_signup_verification_otp(email)
    if otp is None:
        raise HTTPException(
            status_code=400, detail="No pending signup for that email. Start over."
        )
    _email.send_signup_verification_otp(email, otp)
    return JSONResponse(content={"sent": True, "email": email})


@app.post(
    "/auth/signup/verify",
    status_code=201,
    response_model=core_models.AuthRegisterResponse,
    responses=_error_responses(400, 429, 500),
)
@limiter.limit(_AUTH_RATE_LIMIT, key_func=get_remote_address)
def auth_signup_verify(request: Request, body: dict) -> JSONResponse:
    """Verify the OTP and finalize account creation."""
    email = str(body.get("email") or "").strip().lower()
    otp = str(body.get("otp") or "").strip()
    try:
        _auth.init_auth_db()
        result = _auth.consume_signup_verification(email, otp)
    except _auth.SignupVerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _credit_starter_balance(result)
    _email.send_welcome(
        result.get("email", ""),
        result.get("username", "there"),
        role=result.get("role", "both"),
    )
    return JSONResponse(
        content={**result, **_auth_legal_payload(result)}, status_code=201
    )


@app.post(
    "/auth/forgot-password",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(429, 500),
)
@limiter.limit("5/minute", key_func=get_remote_address)
def auth_forgot_password(request: Request, body: dict) -> JSONResponse:
    """Request a password reset OTP. Always returns 200 to avoid leaking account existence."""
    email = str(body.get("email") or "").strip().lower()
    otp = _auth.create_password_reset_token(email)
    if otp:
        user_email = email
        _email.send_password_reset_otp(user_email, otp)
    return JSONResponse(content={"sent": True})


@app.post(
    "/auth/reset-password",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 429, 500),
)
@limiter.limit("10/minute", key_func=get_remote_address)
def auth_reset_password(request: Request, body: dict) -> JSONResponse:
    """Verify OTP and set a new password. Revokes all existing sessions."""
    email = str(body.get("email") or "").strip().lower()
    otp = str(body.get("otp") or "").strip()
    new_password = str(body.get("new_password") or "")
    try:
        _auth.consume_password_reset_token(email, otp, new_password)
    except _auth.PasswordResetError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content={"reset": True})


# ---------------------------------------------------------------------------
# Registry routes
# ---------------------------------------------------------------------------
