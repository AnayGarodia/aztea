# server.application shard 5 — verification, settlement, and dispute
# adjudication: output-verifier calls, registration verifier, quality-gate
# judge, dispute effects, cascaded child-job failure, dispute-window math,
# successful/failed settlement, judge resolution, reputation decay, endpoint
# health probes, and auto-suspension of low-performing agents. No HTTP routes.


def _list_job_events(caller: core_models.CallerContext, since: int | None = None, limit: int = 100) -> list[dict]:
    limit = min(max(1, limit), 200)
    params: list[Any] = []
    where_clauses = []
    if caller["type"] != "master":
        where_clauses.append("(caller_owner_id = ? OR agent_owner_id = ?)")
        params.extend([caller["owner_id"], caller["owner_id"]])
    if since is not None:
        where_clauses.append("event_id > ?")
        params.append(since)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    params.append(limit)
    with jobs._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM job_events
            {where_sql}
            ORDER BY event_id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_event_row_to_dict(r) for r in rows]


def _run_output_verifier(
    verifier_url: str | None,
    *,
    job: dict,
    output_payload: dict,
    timeout_seconds: int = 10,
) -> tuple[bool, str]:
    target = str(verifier_url or "").strip()
    if not target:
        return True, "no external verifier configured"
    try:
        safe_url = _validate_outbound_url(target, "output_verifier_url")
    except ValueError as exc:
        return False, f"invalid verifier url: {exc}"
    payload = {
        "job_id": job["job_id"],
        "agent_id": job["agent_id"],
        "input_payload": job.get("input_payload") or {},
        "output_payload": output_payload,
    }
    try:
        response = http.post(
            safe_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_seconds,
            allow_redirects=False,
        )
        if 300 <= int(response.status_code) < 400:
            return False, "external verifier redirects are not allowed"
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        _LOG.warning("External verifier failed for job %s: %s", job.get("job_id"), exc)
        return False, "external verifier request failed"
    if not isinstance(body, dict):
        return False, "external verifier returned non-object response"
    if bool(body.get("verified")):
        return True, "external verifier passed"
    return False, str(body.get("reason") or "external verifier returned verified=false")


def _run_registration_verifier(
    verifier_url: str | None,
    *,
    registration_payload: dict[str, Any],
    timeout_seconds: int = 10,
) -> tuple[bool, str]:
    target = str(verifier_url or "").strip()
    if not target:
        return False, "no verifier configured"
    try:
        safe_url = _validate_outbound_url(target, "output_verifier_url")
    except ValueError as exc:
        return False, f"invalid verifier url: {exc}"
    payload = {
        "event_type": "agent_registration_verification",
        "agent": registration_payload,
    }
    try:
        response = http.post(
            safe_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_seconds,
            allow_redirects=False,
        )
        if 300 <= int(response.status_code) < 400:
            return False, "registration verifier redirects are not allowed"
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        _LOG.warning("Agent registration verifier request failed for %s: %s", registration_payload.get("name"), exc)
        return False, "registration verifier request failed"
    if not isinstance(body, dict):
        return False, "registration verifier returned non-object response"
    if bool(body.get("verified")):
        return True, str(body.get("reason") or "registration verifier passed")
    return False, str(body.get("reason") or "registration verifier returned verified=false")


def _timeout_error_payload(job_payload: dict) -> dict:
    return error_codes.make_error(
        error_codes.AGENT_TIMEOUT,
        "Job lease expired before completion.",
        {"job": job_payload},
    )


def _run_quality_gate(job: dict, agent: dict, output_payload: dict) -> dict[str, Any]:
    judge_agent_id = str(job.get("judge_agent_id") or _QUALITY_JUDGE_AGENT_ID).strip() or _QUALITY_JUDGE_AGENT_ID
    judge_job_id: str | None = None
    try:
        judge_agent = registry.get_agent(judge_agent_id)
        platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
        judge_wallet = payments.get_or_create_wallet(f"agent:{judge_agent_id}")
        child_charge_tx = payments.pre_call_charge(platform_wallet["wallet_id"], 0, judge_agent_id)
        child = jobs.create_job(
            agent_id=judge_agent_id,
            caller_owner_id="system:quality-judge",
            caller_wallet_id=platform_wallet["wallet_id"],
            agent_wallet_id=judge_wallet["wallet_id"],
            platform_wallet_id=platform_wallet["wallet_id"],
            price_cents=0,
            charge_tx_id=child_charge_tx,
            input_payload={
                "parent_job_id": job["job_id"],
                "input_payload": job.get("input_payload") or {},
                "output_payload": output_payload,
                "agent_description": str(agent.get("description") or ""),
            },
            agent_owner_id=(judge_agent or {}).get("owner_id") or "master",
            max_attempts=1,
            parent_job_id=job["job_id"],
            parent_cascade_policy="detach",
            dispute_window_hours=1,
            judge_agent_id=None,
        )
        judge_job_id = child["job_id"]
    except Exception:
        judge_job_id = None

    output_schema = agent.get("output_schema")
    has_output_schema = output_schema is not None
    live_quality_toggle = (
        os.environ.get("AZTEA_ENABLE_LIVE_QUALITY_JUDGE")
        or os.environ.get("AGENTMARKET_ENABLE_LIVE_QUALITY_JUDGE")
        or ""
    )
    live_quality_enabled = (
        str(live_quality_toggle).strip().lower() in {"1", "true", "yes", "on"}
        and bool(str(os.environ.get("GROQ_API_KEY", "")).strip())
    )

    verdict = "pass"
    score = 5
    reason = "No output contract defined. Structural check passed."
    parsed_output: Any
    try:
        parsed_output = json.loads(_stable_json_text(output_payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed_output = None
        verdict = "fail"
        score = 0
        reason = "Output payload was not valid JSON."

    if verdict == "pass" and (parsed_output is None or parsed_output == {}):
        verdict = "fail"
        score = 0
        reason = "Output payload must not be null or an empty object."

    if verdict == "pass" and has_output_schema and isinstance(output_schema, dict):
        schema_errors = _validate_json_schema_subset(parsed_output, output_schema)
        if schema_errors:
            verdict = "fail"
            score = 0
            reason = f"Output did not match declared schema: {schema_errors[0]}"
        else:
            reason = "Output matched declared schema and structural checks."

    if verdict == "pass" and live_quality_enabled:
        try:
            judge_result = judges.run_quality_judgment(
                input_payload=job.get("input_payload") or {},
                output_payload=output_payload,
                agent_description=str(agent.get("description") or ""),
            )
            judge_verdict = str(judge_result.get("verdict") or "").strip().lower()
            if judge_verdict in {"pass", "fail"}:
                verdict = judge_verdict
            else:
                verdict = "fail"
            try:
                score = int(judge_result.get("score"))
            except (TypeError, ValueError):
                score = 1 if verdict == "fail" else 5
            score = max(0, min(10, score))
            reason = str(judge_result.get("reason") or "").strip() or "Quality judge returned no reason."
        except Exception as exc:
            verdict = "fail"
            score = 0
            reason = f"quality judge error: {exc}"

    verifier_passed, verifier_reason = _run_output_verifier(
        agent.get("output_verifier_url"),
        job=job,
        output_payload=output_payload,
    )
    if verdict == "pass" and not verifier_passed:
        verdict = "fail"
        reason = f"{reason} External verifier: {verifier_reason}"

    if judge_job_id is not None:
        child_output = {"verdict": verdict, "score": score, "reason": reason}
        child_complete = jobs.update_job_status(judge_job_id, "complete", output_payload=child_output, completed=True)
        if child_complete is not None:
            jobs.mark_settled(judge_job_id)

    passed = verdict == "pass"
    return {
        "judge_agent_id": judge_agent_id,
        "judge_job_id": judge_job_id,
        "judge_verdict": verdict,
        "quality_score": score,
        "reason": reason,
        "passed": passed,
        "verifier_reason": verifier_reason,
    }


def _apply_dispute_effects(dispute: dict, outcome: str) -> None:
    normalized_outcome = str(outcome or "").strip().lower()
    current_job = jobs.get_job(dispute["job_id"])
    was_settled = bool((current_job or {}).get("settled_at"))
    previous_outcome = str((current_job or {}).get("dispute_outcome") or "").strip().lower()
    job = jobs.set_job_dispute_outcome(dispute["job_id"], normalized_outcome)
    if job is None:
        return
    if not was_settled:
        jobs.mark_settled(dispute["job_id"])
        job = jobs.get_job(dispute["job_id"]) or job
    if normalized_outcome == "caller_wins" and previous_outcome != "caller_wins":
        registry.update_call_stats(job["agent_id"], latency_ms=0.0, success=False)
    elif normalized_outcome in {"agent_wins", "split", "void"} and not was_settled:
        registry.update_call_stats(job["agent_id"], latency_ms=_job_latency_ms(job), success=True)

    filed_by = str(dispute.get("filed_by_owner_id") or "").strip()
    if filed_by.startswith("user:") and dispute.get("side") == "caller" and normalized_outcome == "agent_wins":
        payments.adjust_caller_trust_once(
            filed_by,
            delta=-0.05,
            reason="dispute_loss",
            related_id=dispute["dispute_id"],
        )


def _fail_open_jobs_for_agent(agent_id: str, actor_owner_id: str, reason: str) -> dict[str, int]:
    affected = 0
    refunded = 0
    for status in ("pending", "running", "awaiting_clarification"):
        open_jobs = jobs.list_jobs_for_agent(agent_id, status=status, limit=500)
        for item in open_jobs:
            updated = jobs.update_job_status(
                item["job_id"],
                "failed",
                error_message=reason,
                completed=True,
            )
            if updated is None:
                continue
            affected += 1
            settled = _settle_failed_job(updated, actor_owner_id=actor_owner_id, event_type="job.failed_agent_banned")
            if settled.get("settled_at"):
                refunded += 1
    return {"affected_jobs": affected, "refunded_jobs": refunded}


def _normalize_output_verification_status(job: dict) -> str:
    status = str(job.get("output_verification_status") or "").strip().lower()
    if status in {"pending", "accepted", "rejected", "expired"}:
        return status
    return "not_required"


def _ensure_output_rejection_dispute(
    job: dict,
    *,
    filed_by_owner_id: str,
    reason: str,
    evidence: str | None = None,
) -> dict:
    existing = disputes.get_dispute_by_job(job["job_id"])
    if existing is not None:
        return existing

    conn = payments._conn()
    filing_deposit_cents = _compute_dispute_filing_deposit_cents(int(job.get("price_cents") or 0))
    insufficient_phase = "dispute_create"
    try:
        conn.execute("BEGIN IMMEDIATE")
        created = disputes.create_dispute(
            job_id=job["job_id"],
            filed_by_owner_id=filed_by_owner_id,
            side="caller",
            reason=reason,
            evidence=evidence,
            filing_deposit_cents=filing_deposit_cents,
            conn=conn,
        )
        insufficient_phase = "filing_deposit"
        payments.collect_dispute_filing_deposit(
            created["dispute_id"],
            filed_by_owner_id=filed_by_owner_id,
            amount_cents=filing_deposit_cents,
            conn=conn,
        )
        insufficient_phase = "clawback_lock"
        payments.lock_dispute_funds(created["dispute_id"], conn=conn)
        conn.execute("COMMIT")
        return created
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        existing = disputes.get_dispute_by_job(job["job_id"])
        if existing is not None:
            return existing
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


def _cascade_fail_active_child_jobs(parent_job: dict, actor_owner_id: str) -> dict[str, Any]:
    active_children = jobs.list_child_jobs(
        parent_job["job_id"],
        statuses=("pending", "running", "awaiting_clarification"),
        limit=500,
    )
    failed_child_job_ids: list[str] = []
    for child in active_children:
        policy = str(child.get("parent_cascade_policy") or "").strip().lower() or "detach"
        if policy != "fail_children_on_parent_fail":
            continue
        updated = jobs.update_job_status(
            child["job_id"],
            "failed",
            error_message=f"Parent job {parent_job['job_id']} failed; child was cascaded.",
            completed=True,
        )
        if updated is None:
            continue
        settled_child = _settle_failed_job(
            updated,
            actor_owner_id=actor_owner_id,
            event_type="job.failed_parent_cascade",
            refund_fraction=1.0,
        )
        failed_child_job_ids.append(settled_child["job_id"])
    return {
        "scanned_children": len(active_children),
        "failed_children": len(failed_child_job_ids),
        "failed_child_job_ids": failed_child_job_ids,
    }


def _effective_dispute_window_seconds(job: dict) -> int:
    dispute_window_hours = _to_non_negative_int(
        job.get("dispute_window_hours"),
        default=_DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
    )
    if dispute_window_hours < 1:
        dispute_window_hours = _DEFAULT_JOB_DISPUTE_WINDOW_HOURS
    configured_window_seconds = dispute_window_hours * 3600
    return min(configured_window_seconds, _DISPUTE_FILE_WINDOW_SECONDS)


def _dispute_window_deadline(job: dict) -> datetime | None:
    completed_at = _parse_iso_datetime(job.get("completed_at"))
    if completed_at is None:
        return None
    return completed_at + timedelta(seconds=_effective_dispute_window_seconds(job))


def _is_dispute_window_open(job: dict, *, now_dt: datetime | None = None) -> bool:
    deadline = _dispute_window_deadline(job)
    if deadline is None:
        return False
    current = now_dt or datetime.now(timezone.utc)
    return current <= deadline


def _settle_successful_job(
    job: dict,
    actor_owner_id: str,
    *,
    require_dispute_window_expiry: bool = True,
) -> dict:
    newly_settled = False
    refreshed = jobs.initialize_output_verification_state(job["job_id"])
    if refreshed is not None:
        job = refreshed
    if disputes.has_dispute_for_job(job["job_id"]):
        return jobs.get_job(job["job_id"]) or job
    verification_status = _normalize_output_verification_status(job)
    if verification_status == "pending":
        return jobs.get_job(job["job_id"]) or job
    if verification_status == "rejected":
        return jobs.get_job(job["job_id"]) or job
    # Explicit caller acceptance should release funds immediately; only implicit acceptance
    # paths remain gated by the dispute window timeout.
    if require_dispute_window_expiry and _is_dispute_window_open(job):
        return jobs.get_job(job["job_id"]) or job
    if not job["settled_at"]:
        payments.post_call_payout(
            job["agent_wallet_id"],
            job["platform_wallet_id"],
            job["charge_tx_id"],
            job["price_cents"],
            job["agent_id"],
            platform_fee_pct=job.get("platform_fee_pct_at_create"),
            fee_bearer_policy=job.get("fee_bearer_policy"),
        )
        newly_settled = jobs.mark_settled(job["job_id"])
        if newly_settled:
            registry.update_call_stats(job["agent_id"], latency_ms=_job_latency_ms(job), success=True)
    settled = jobs.get_job(job["job_id"]) or job
    if newly_settled:
        _record_job_event(
            settled,
            "job.settled",
            actor_owner_id=actor_owner_id,
            payload={"status": settled["status"], "settled_at": settled.get("settled_at")},
        )
    return settled


def _settle_failed_job(
    job: dict,
    actor_owner_id: str,
    event_type: str = "job.failed",
    refund_fraction: float = 1.0,
) -> dict:
    newly_settled = False
    if not job["settled_at"]:
        refund_fraction = max(0.0, min(1.0, float(refund_fraction)))
        if refund_fraction >= 1.0:
            # Full refund — original fast path
            payments.post_call_refund(
                job["caller_wallet_id"],
                job["charge_tx_id"],
                int(job.get("caller_charge_cents") or job["price_cents"]),
                job["agent_id"],
            )
        else:
            # Partial settle: refund fraction to caller, keep rest for agent
            payments.post_call_partial_settle(
                caller_wallet_id=job["caller_wallet_id"],
                agent_wallet_id=job["agent_wallet_id"],
                platform_wallet_id=job["platform_wallet_id"],
                charge_tx_id=job["charge_tx_id"],
                price_cents=job["price_cents"],
                refund_fraction=refund_fraction,
                agent_id=job["agent_id"],
                platform_fee_pct=job.get("platform_fee_pct_at_create"),
                fee_bearer_policy=job.get("fee_bearer_policy"),
                caller_charge_cents=job.get("caller_charge_cents"),
            )
        newly_settled = jobs.mark_settled(job["job_id"])
        if newly_settled:
            registry.update_call_stats(job["agent_id"], latency_ms=_job_latency_ms(job), success=False)
    settled = jobs.get_job(job["job_id"]) or job
    if newly_settled:
        _record_job_event(
            settled,
            event_type,
            actor_owner_id=actor_owner_id,
            payload={"status": settled["status"], "error_message": settled.get("error_message")},
        )
        try:
            caller_email = _get_owner_email(settled.get("caller_owner_id", ""))
            if caller_email:
                _agent_name = (registry.get_agent(settled["agent_id"]) or {}).get("name", settled["agent_id"])
                _email.send_job_failed(caller_email, settled["job_id"], _agent_name, settled.get("error_message") or "")
        except Exception as exc:
            _LOG.warning("Failed to send job failure email for job %s: %s", settled.get("job_id"), exc)
    if (
        str(settled.get("status") or "").strip().lower() == "failed"
    ):
        _cascade_fail_active_child_jobs(settled, actor_owner_id=actor_owner_id)
    return settled


def _dispute_view(dispute_row: dict) -> dict:
    payload = dict(dispute_row)
    payload["judgments"] = disputes.get_judgments(payload["dispute_id"])
    return payload


def _dispute_side_for_caller(caller: core_models.CallerContext, job: dict) -> str:
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Master key cannot file disputes.")
    owner_id = caller["owner_id"]
    if owner_id == job["caller_owner_id"]:
        return "caller"
    if _caller_worker_authorized_for_job(caller, job):
        return "agent"
    raise HTTPException(status_code=403, detail="Only the caller or agent owner can file this dispute.")


def _resolve_dispute_with_judges(dispute_id: str, actor_owner_id: str) -> tuple[dict, dict | None]:
    result = judges.run_judgment(dispute_id)
    status = str(result.get("status") or "").strip().lower()
    outcome = result.get("outcome")
    settlement = None

    if status == "consensus" and outcome:
        dispute_row = disputes.get_dispute(dispute_id)
        if dispute_row is None:
            raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
        settlement = payments.post_dispute_settlement(
            dispute_id,
            outcome=outcome,
            split_caller_cents=dispute_row.get("split_caller_cents"),
            split_agent_cents=dispute_row.get("split_agent_cents"),
        )
        disputes.finalize_dispute(
            dispute_id,
            status="resolved",
            outcome=outcome,
            split_caller_cents=dispute_row.get("split_caller_cents"),
            split_agent_cents=dispute_row.get("split_agent_cents"),
        )
        latest_dispute = disputes.get_dispute(dispute_id)
        if latest_dispute is not None:
            _apply_dispute_effects(latest_dispute, outcome)
    elif status == "tied":
        disputes.set_dispute_tied(dispute_id)

    latest = disputes.get_dispute(dispute_id)
    if latest is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
    job = jobs.get_job(latest["job_id"])
    if job is not None:
        _record_job_event(
            job,
            "job.dispute_judged",
            actor_owner_id=actor_owner_id,
            payload={"dispute_id": dispute_id, "status": latest["status"], "outcome": latest.get("outcome")},
        )
    return _dispute_view(latest), settlement


def _apply_reputation_decay(now_dt: datetime | None = None) -> dict[str, int]:
    current = now_dt or datetime.now(timezone.utc)
    scanned = 0
    decayed = 0
    with jobs._conn() as conn:
        rows = conn.execute(
            """
            SELECT
                a.agent_id,
                a.trust_decay_multiplier,
                a.last_decay_at,
                a.total_calls,
                MAX(j.completed_at) AS last_completed_at,
                a.created_at
            FROM agents a
            LEFT JOIN jobs j
              ON j.agent_id = a.agent_id
             AND j.status = 'complete'
             AND j.completed_at IS NOT NULL
            WHERE a.status = 'active'
            GROUP BY a.agent_id
            """
        ).fetchall()
    for row in rows:
        scanned += 1
        # Skip decay when there isn't enough signal — penalizing new agents is unfair.
        if (row["total_calls"] or 0) < 20:
            continue
        reference = _parse_iso_datetime(row["last_completed_at"]) or _parse_iso_datetime(row["created_at"])
        if reference is None:
            continue
        decay_threshold = reference + timedelta(days=_REPUTATION_DECAY_GRACE_DAYS)
        if current <= decay_threshold:
            continue
        last_decay_at = _parse_iso_datetime(row["last_decay_at"]) or decay_threshold
        start = decay_threshold if last_decay_at < decay_threshold else last_decay_at
        elapsed_days = int((current - start).total_seconds() // 86400)
        if elapsed_days <= 0:
            continue
        current_multiplier = max(0.0, min(1.0, float(row["trust_decay_multiplier"] or 1.0)))
        new_multiplier = current_multiplier * ((1.0 - _REPUTATION_DECAY_DAILY_RATE) ** elapsed_days)
        new_multiplier = max(0.0, min(1.0, new_multiplier))
        if new_multiplier >= current_multiplier:
            continue
        registry.set_agent_decay_multiplier(row["agent_id"], new_multiplier, current.isoformat())
        decayed += 1
    return {"scanned_agents": scanned, "decayed_agents": decayed}


def _should_monitor_agent_endpoint(agent: dict) -> bool:
    status = str(agent.get("status") or "").strip().lower()
    if status in {"banned", "suspended"}:
        return False
    endpoint = str(agent.get("endpoint_url") or "").strip()
    if not endpoint:
        return False
    if endpoint.startswith("internal://"):
        return False
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").strip().lower()
    if host in {"example.com"} or host.endswith(".example.com"):
        return False
    if host.endswith(".test") or host.endswith(".invalid"):
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _probe_agent_endpoint_health(endpoint_url: str, timeout_seconds: int) -> tuple[bool, str | None]:
    safe_url = _validate_outbound_url(str(endpoint_url or "").strip(), "endpoint_url")
    response = http.head(safe_url, timeout=timeout_seconds, allow_redirects=False)
    status_code = int(response.status_code)
    if status_code in {405, 501}:
        response = http.get(safe_url, timeout=timeout_seconds, allow_redirects=False)
        status_code = int(response.status_code)
    if 200 <= status_code < 500:
        return True, None
    return False, f"status_code={status_code}"


def _monitor_agent_endpoints(
    *,
    limit: int = _ENDPOINT_MONITOR_BATCH_SIZE,
    timeout_seconds: int = _ENDPOINT_MONITOR_TIMEOUT_SECONDS,
    failure_threshold: int = _ENDPOINT_MONITOR_FAILURE_THRESHOLD,
) -> dict[str, Any]:
    agents = registry.get_agents(include_internal=True, include_banned=True)
    checked = 0
    healthy = 0
    degraded = 0
    recovered = 0
    degraded_agent_ids: list[str] = []
    recovered_agent_ids: list[str] = []
    for agent in agents:
        if checked >= limit:
            break
        if not _should_monitor_agent_endpoint(agent):
            continue
        checked += 1
        agent_id = str(agent.get("agent_id") or "")
        previous_status = str(agent.get("endpoint_health_status") or "unknown").strip().lower()
        previous_failures = _to_non_negative_int(agent.get("endpoint_consecutive_failures"), default=0)
        endpoint_url = str(agent.get("endpoint_url") or "").strip()
        ok = False
        error_text: str | None = None
        try:
            ok, error_text = _probe_agent_endpoint_health(endpoint_url, timeout_seconds=timeout_seconds)
        except Exception as exc:
            ok = False
            error_text = str(exc) or "endpoint health check failed"
        if ok:
            new_failures = 0
            new_status = "healthy"
            healthy += 1
            if previous_status == "degraded":
                recovered += 1
                recovered_agent_ids.append(agent_id)
        else:
            new_failures = previous_failures + 1
            new_status = "degraded" if new_failures >= failure_threshold else "healthy"
            if new_status == "degraded":
                degraded += 1
                if previous_status != "degraded":
                    degraded_agent_ids.append(agent_id)
        registry.set_agent_endpoint_health(
            agent_id,
            endpoint_health_status=new_status,
            endpoint_consecutive_failures=new_failures,
            endpoint_last_checked_at=_utc_now_iso(),
            endpoint_last_error=None if ok else error_text,
        )
    return {
        "endpoint_checks_scanned": checked,
        "endpoint_healthy_count": healthy,
        "endpoint_degraded_count": degraded,
        "endpoint_recovered_count": recovered,
        "endpoint_degraded_agent_ids": degraded_agent_ids,
        "endpoint_recovered_agent_ids": recovered_agent_ids,
    }


def _auto_suspend_low_performing_agents(actor_owner_id: str) -> dict[str, Any]:
    suspended_agent_ids: list[str] = []
    generated_events: list[dict[str, Any]] = []
    now_iso = _utc_now_iso()
    with jobs._conn() as conn:
        rows = conn.execute(
            """
            SELECT agent_id, owner_id, successful_calls, total_calls
            FROM agents
            WHERE status = 'active' AND total_calls >= ?
            """,
            (AUTO_SUSPEND_MIN_CALLS,),
        ).fetchall()
        for row in rows:
            total_calls = int(row["total_calls"] or 0)
            successful_calls = int(row["successful_calls"] or 0)
            if total_calls <= 0:
                continue
            failure_rate = 1.0 - (float(successful_calls) / float(total_calls))
            if failure_rate <= AUTO_SUSPEND_FAILURE_RATE_THRESHOLD:
                continue
            status_update = conn.execute(
                "UPDATE agents SET status = 'suspended' WHERE agent_id = ? AND status = 'active'",
                (row["agent_id"],),
            )
            if status_update.rowcount <= 0:
                continue

            payload = {
                "reason": "failure_rate_threshold",
                "failure_rate": round(failure_rate, 4),
                "total_calls": total_calls,
            }
            cursor = conn.execute(
                """
                INSERT INTO job_events
                    (job_id, agent_id, agent_owner_id, caller_owner_id, event_type, actor_owner_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"agent:{row['agent_id']}",
                    row["agent_id"],
                    row["owner_id"] or "unknown",
                    "system:sweeper",
                    "agent_auto_suspended",
                    actor_owner_id,
                    _stable_json_text(payload),
                    now_iso,
                ),
            )
            event = {
                "event_id": int(cursor.lastrowid),
                "job_id": f"agent:{row['agent_id']}",
                "agent_id": str(row["agent_id"]),
                "agent_owner_id": str(row["owner_id"] or "unknown"),
                "caller_owner_id": "system:sweeper",
                "event_type": "agent_auto_suspended",
                "actor_owner_id": actor_owner_id,
                "payload": payload,
                "created_at": now_iso,
            }
            generated_events.append(event)
            suspended_agent_ids.append(str(row["agent_id"]))
    for event in generated_events:
        _deliver_job_event_hooks(event)
    return {
        "auto_suspended_count": len(suspended_agent_ids),
        "auto_suspended_agent_ids": suspended_agent_ids,
    }


