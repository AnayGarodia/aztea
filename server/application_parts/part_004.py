# server.application shard 4 — built-in agent execution (no-HTTP routing for
# internal agents), builtin worker + dispute judge + endpoint health +
# payments reconciliation + hook-delivery background loops, and the job
# callback / hook delivery pipeline. No HTTP routes here.


from server.pricing_helpers import (  # noqa: E402
    builtin_pricing_overlay as _builtin_pricing_overlay,  # noqa: F401
)
from server.pricing_helpers import (
    estimate_variable_charge as _estimate_variable_charge,  # noqa: F401
)
from server.pricing_helpers import (
    maybe_refund_pricing_diff as _maybe_refund_pricing_diff,  # noqa: F401
)
from server.pricing_helpers import (
    resolve_agent_pricing as _resolve_agent_pricing,  # noqa: F401
)
import sqlite3


def _validate_builtin_agent_payload(
    agent_id: str, input_payload: dict[str, Any]
) -> None:
    """Pre-charge validation hook for Pydantic-model-backed builtins.

    All sunset agents that needed pre-charge validation have been removed;
    every remaining builtin validates inside its own run() and returns an
    error dict rather than raising, so this is a no-op kept as a future
    extension point. New agents that need pre-charge raise-on-invalid
    validation should add their case here.
    """
    return


# Per-agent concurrency caps. Without these, par>=25 fan-outs against
# heavy agents (web_search, git_diff_analyzer, browser_agent) saturate
# their internal pools and surface as 502 agent.endpoint_misconfigured.
# We now fail fast at the dispatch boundary with a clean rate-limit
# rejection so the caller's batch infrastructure refunds and retries.
# Heavy agents (subprocess/Playwright/HTTP-fanout) get tighter caps;
# pure-CPU helpers can run wide.
_AGENT_CONCURRENCY_DEFAULT = int(os.environ.get("AZTEA_AGENT_CONCURRENCY_DEFAULT", "16"))
_AGENT_CONCURRENCY_LIMITS: dict[str, int] = {
    _PYTHON_EXECUTOR_AGENT_ID: 16,
    _MULTI_LANGUAGE_EXECUTOR_AGENT_ID: 8,
    _BROWSER_AGENT_ID: 4,
    _VISUAL_REGRESSION_AGENT_ID: 4,
    _LIGHTHOUSE_AUDITOR_AGENT_ID: 4,
    _ACCESSIBILITY_AUDITOR_AGENT_ID: 4,
    _BROKEN_LINK_CRAWLER_AGENT_ID: 4,
    _DB_SANDBOX_AGENT_ID: 8,
}
_AGENT_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_AGENT_SEMAPHORES_LOCK = threading.Lock()
# Acquire wait — short, because the caller's batch is timing-sensitive and
# a hard 429 lets them refund and retry next tick. Tunable via env so ops
# can lengthen for slower-but-cheaper backpressure if needed.
_AGENT_SEMAPHORE_WAIT_SECONDS = float(
    os.environ.get("AZTEA_AGENT_SEMAPHORE_WAIT_SECONDS", "0.5")
)


class _AgentSlotUnavailable(Exception):
    """Raised when an agent's concurrency cap is saturated. Caller must
    refund and surface a 429 with `agent.upstream_timeout`."""

    def __init__(self, agent_id: str, limit: int):
        self.agent_id = agent_id
        self.limit = limit
        super().__init__(
            f"Agent '{agent_id}' is at its concurrency cap ({limit} in flight)."
        )


def _agent_semaphore(agent_id: str) -> threading.BoundedSemaphore:
    sem = _AGENT_SEMAPHORES.get(agent_id)
    if sem is not None:
        return sem
    with _AGENT_SEMAPHORES_LOCK:
        sem = _AGENT_SEMAPHORES.get(agent_id)
        if sem is not None:
            return sem
        limit = max(
            1, _AGENT_CONCURRENCY_LIMITS.get(agent_id, _AGENT_CONCURRENCY_DEFAULT)
        )
        sem = threading.BoundedSemaphore(limit)
        _AGENT_SEMAPHORES[agent_id] = sem
        return sem


def _execute_builtin_agent(agent_id: str, input_payload: dict[str, Any]) -> dict:
    def _finalize(output: Any) -> dict:
        if isinstance(output, dict) and isinstance(output.get("error"), dict):
            return output
        if not isinstance(output, dict):
            output = {"result": output}
        result = dict(output)
        result.setdefault("billing_units_actual", 1)
        result.setdefault("degraded_mode", False)
        if "llm_used" not in result:
            meta = _builtin_specs.builtin_catalog_metadata(agent_id) or {}
            runtime_requirements = [
                str(item).lower() for item in meta.get("runtime_requirements") or []
            ]
            result["llm_used"] = (
                False
                if result.get("degraded_mode")
                else any("llm provider" in item for item in runtime_requirements)
            )
        result.setdefault("agent_contract_version", "builtin-v2")
        return result

    payload = input_payload or {}

    sem = _agent_semaphore(agent_id)
    if not sem.acquire(timeout=_AGENT_SEMAPHORE_WAIT_SECONDS):
        raise _AgentSlotUnavailable(
            agent_id,
            _AGENT_CONCURRENCY_LIMITS.get(agent_id, _AGENT_CONCURRENCY_DEFAULT),
        )
    try:
        return _execute_builtin_agent_inner(agent_id, payload, _finalize)
    finally:
        sem.release()


def _execute_builtin_agent_inner(
    agent_id: str, payload: dict[str, Any], _finalize
) -> dict:
    if agent_id == _QUALITY_JUDGE_AGENT_ID:
        return _finalize(
            judges.run_quality_judgment(
                input_payload=payload.get("input_payload")
                if isinstance(payload, dict)
                else {},
                output_payload=payload.get("output_payload")
                if isinstance(payload, dict)
                else {},
                agent_description=str(payload.get("agent_description") or "")
                if isinstance(payload, dict)
                else "",
            )
        )
    if agent_id == _CVELOOKUP_AGENT_ID:
        return _finalize(agent_cve_lookup.run(payload))
    if agent_id == _PYTHON_EXECUTOR_AGENT_ID:
        return _finalize(agent_python_executor.run(payload))
    if agent_id == _DNS_INSPECTOR_AGENT_ID:
        return _finalize(agent_dns_inspector.run(payload))
    if agent_id == _DEPENDENCY_AUDITOR_AGENT_ID:
        return _finalize(agent_dependency_auditor.run(payload))
    if agent_id == _DB_SANDBOX_AGENT_ID:
        return _finalize(agent_db_sandbox.run(payload))
    if agent_id == _VISUAL_REGRESSION_AGENT_ID:
        return _finalize(agent_visual_regression.run(payload))
    if agent_id == _BROWSER_AGENT_ID:
        return _finalize(agent_browser_agent.run(payload))
    if agent_id == _MULTI_LANGUAGE_EXECUTOR_AGENT_ID:
        return _finalize(agent_multi_language_executor.run(payload))
    if agent_id == _SECRET_SCANNER_AGENT_ID:
        return _finalize(agent_secret_scanner.run(payload))
    if agent_id == _LIGHTHOUSE_AUDITOR_AGENT_ID:
        return _finalize(agent_lighthouse_auditor.run(payload))
    if agent_id == _ACCESSIBILITY_AUDITOR_AGENT_ID:
        return _finalize(agent_accessibility_auditor.run(payload))
    if agent_id == _SECURITY_HEADERS_GRADER_AGENT_ID:
        return _finalize(agent_security_headers_grader.run(payload))
    if agent_id == _BROKEN_LINK_CRAWLER_AGENT_ID:
        return _finalize(agent_broken_link_crawler.run(payload))
    if agent_id == _PDF_DOCUMENT_PARSER_AGENT_ID:
        return _finalize(agent_pdf_document_parser.run(payload))
    if agent_id == _WEB_SEARCH_AGENT_ID:
        return _finalize(agent_web_search.run(payload))
    if agent_id == _DOCS_GROUNDER_AGENT_ID:
        return _finalize(agent_docs_grounder.run(payload))
    if agent_id == _SAST_SCANNER_AGENT_ID:
        return _finalize(agent_sast_scanner.run(payload))
    if agent_id == _STRIPE_WEBHOOK_DEBUGGER_AGENT_ID:
        return _finalize(agent_stripe_webhook_debugger.run(payload))
    if agent_id == _LOAD_TESTER_AGENT_ID:
        return _finalize(agent_load_tester.run(payload))
    if agent_id == _CI_FAILURE_REPRODUCER_AGENT_ID:
        return _finalize(agent_ci_failure_reproducer.run(payload))
    raise ValueError(f"Unsupported built-in agent '{agent_id}'.")


# No agents currently use the degraded-unchargeable path; keep the set
# empty so _is_unchargeable_degraded_output() always returns False until
# a new agent that needs it is added.
_DEGRADED_UNCHARGEABLE_AGENT_IDS: set[str] = set()


def _is_unchargeable_degraded_output(agent_id: str, output: Any) -> bool:
    if agent_id not in _DEGRADED_UNCHARGEABLE_AGENT_IDS:
        return False
    if not isinstance(output, dict):
        return False
    if bool(output.get("degraded_chargeable")):
        return False
    return bool(output.get("degraded_mode")) and not bool(output.get("llm_used"))


def _degraded_unchargeable_error(agent_id: str) -> dict[str, Any]:
    return {
        "error": {
            "code": "agent.degraded_unavailable",
            "message": (
                "The agent could not reach its required synthesis provider and "
                "only produced degraded fallback output; no charge was kept."
            ),
        },
        "agent_id": agent_id,
        "degraded_mode": True,
        "llm_used": False,
        "billing_units_actual": 0,
    }


def _sign_builtin_output(agent: dict | None, output: dict) -> dict[str, str | None]:
    sig = {"signature": None, "alg": None, "did": None, "signed_at": None}
    if not agent:
        return sig
    try:
        from core import crypto as _crypto

        private_pem = agent.get("signing_private_key")
        agent_did_value = agent.get("did")
        if not private_pem or not agent_did_value:
            private_pem, _public_pem, agent_did_value = (
                registry.ensure_agent_signing_keys(agent["agent_id"])
            )
        if private_pem and agent_did_value:
            sig["signature"] = _crypto.sign_payload(private_pem, output)
            sig["alg"] = str(agent.get("signing_alg") or "ed25519")
            sig["did"] = agent_did_value
            sig["signed_at"] = datetime.now(timezone.utc).isoformat()
    except Exception:
        _LOG.exception(
            "Failed to sign builtin async output for agent %s",
            (agent or {}).get("agent_id"),
        )
    return sig


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
            skill_row = _hosted_skills.get_hosted_skill_by_agent_id(
                str(claimed["agent_id"])
            )
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
        if _is_unchargeable_degraded_output(str(claimed["agent_id"]), output):
            output = _degraded_unchargeable_error(str(claimed["agent_id"]))
        agent_failed, failure_code, failure_message = _is_agent_failure_envelope(output)
        if agent_failed:
            updated = jobs.update_job_status(
                claimed["job_id"],
                "failed",
                output_payload=output,
                error_message=(
                    failure_message or f"Agent reported {failure_code}; no charge."
                ),
                completed=True,
            )
            if updated is not None:
                _settle_failed_job(
                    updated,
                    actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                    event_type="job.failed_dependency",
                )
            return True
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
                payload={
                    "retry_count": retried["retry_count"],
                    "next_retry_at": retried["next_retry_at"],
                },
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
    except (TimeoutError, ConnectionError, OSError) as exc:
        # Transient I/O. The 2026-05-08 power-user eval saw the first job
        # of a 20-job batch flip to `failed` instantly while the same
        # input succeeded as a single hire moments earlier — almost
        # certainly a one-shot transient (network blip, ephemeral DNS,
        # subprocess pipe). Don't burn a retry budget on first sight.
        # Pre-2026-05-08 this exception class was caught by the broad
        # `except Exception` below and immediately marked terminal-failed.
        retried = jobs.schedule_job_retry(
            claimed["job_id"],
            retry_delay_seconds=_SWEEPER_RETRY_DELAY_SECONDS,
            error_message=f"Built-in worker transient {type(exc).__name__}: {exc}",
            claim_owner_id=_BUILTIN_WORKER_OWNER_ID,
            claim_token=claimed.get("claim_token"),
            require_authorized_owner=False,
        )
        if retried is not None and retried["status"] == "pending":
            _record_job_event(
                retried,
                "job.retry_scheduled",
                actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                payload={
                    "retry_count": retried["retry_count"],
                    "next_retry_at": retried["next_retry_at"],
                    "error_class": type(exc).__name__,
                },
            )
            return True
        updated = retried or jobs.update_job_status(
            claimed["job_id"],
            "failed",
            error_message=f"Built-in worker transient {type(exc).__name__} (retries exhausted): {exc}",
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
        # Unexpected exception class. Stash error_class in the failure
        # record so post-mortems can find these without log-grepping.
        updated = jobs.update_job_status(
            claimed["job_id"],
            "failed",
            error_message=f"Built-in execution failed: {type(exc).__name__}: {exc}",
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
    signature = _sign_builtin_output(agent, output if isinstance(output, dict) else {})
    completed = jobs.update_job_status(
        claimed["job_id"],
        "complete",
        output_payload=output,
        completed=True,
        output_signature=signature["signature"],
        output_signature_alg=signature["alg"],
        output_signed_by_did=signature["did"],
        output_signed_at=signature["signed_at"],
    )
    if completed is not None:
        settled = _settle_successful_job(
            completed, actor_owner_id=_BUILTIN_WORKER_OWNER_ID
        )
        if agent is not None:
            distribution = payments.compute_success_distribution(
                int(completed.get("price_cents") or 0),
                platform_fee_pct=completed.get("platform_fee_pct_at_create"),
                fee_bearer_policy=completed.get("fee_bearer_policy"),
            )
            platform_fee_cents = int(distribution["platform_fee_cents"])
            judge_fee_cents = min(_JUDGE_FEE_CENTS, platform_fee_cents)
            if judge_fee_cents > 0:
                judge_agent_id = str(
                    settled.get("judge_agent_id") or _QUALITY_JUDGE_AGENT_ID
                )
                judge_wallet = payments.get_or_create_wallet(f"agent:{judge_agent_id}")
                payments.record_judge_fee(
                    completed["platform_wallet_id"],
                    judge_wallet["wallet_id"],
                    charge_tx_id=completed["charge_tx_id"],
                    agent_id=completed["agent_id"],
                    fee_cents=judge_fee_cents,
                )
    return True


_BUILTIN_WORKER_POOL_LOCK = threading.Lock()
_BUILTIN_WORKER_POOL: Any = None  # type: ignore[assignment]
_BUILTIN_WORKER_INFLIGHT_LOCK = threading.Lock()
_BUILTIN_WORKER_INFLIGHT_COUNT = 0
_BUILTIN_WORKER_INFLIGHT_IDS: set[str] = set()


def _get_builtin_worker_pool():
    """Return the persistent module-scoped ThreadPoolExecutor.

    A NEW executor was being created and torn down on every worker tick via
    `with ThreadPoolExecutor(...) as pool:`. The `with` block waits for ALL
    submitted futures to complete before returning, so a slow job (e.g.
    browser_agent at ~8s) blocked the next tick from picking up freshly-
    submitted batches — exactly the concurrent-batch stall the eval found
    where a 20-job batch sat at 0/20 for 5+ minutes while another batch
    drained.

    The persistent pool decouples submit from wait: each tick scans pending
    work, submits up to (parallelism - inflight) jobs, increments the
    inflight counter, and returns immediately. Slow jobs occupy worker
    threads but never block scheduling.
    """
    from concurrent.futures import ThreadPoolExecutor

    global _BUILTIN_WORKER_POOL
    if _BUILTIN_WORKER_POOL is not None:
        return _BUILTIN_WORKER_POOL
    with _BUILTIN_WORKER_POOL_LOCK:
        if _BUILTIN_WORKER_POOL is None:
            _BUILTIN_WORKER_POOL = ThreadPoolExecutor(
                max_workers=max(1, int(_BUILTIN_JOB_WORKER_PARALLELISM)),
                thread_name_prefix="aztea-builtin",
            )
    return _BUILTIN_WORKER_POOL


def _builtin_worker_inflight_snapshot() -> tuple[int, int]:
    """(in_flight_count, capacity_remaining)."""
    with _BUILTIN_WORKER_INFLIGHT_LOCK:
        return (
            _BUILTIN_WORKER_INFLIGHT_COUNT,
            max(
                0,
                int(_BUILTIN_JOB_WORKER_PARALLELISM) - _BUILTIN_WORKER_INFLIGHT_COUNT,
            ),
        )


def _process_pending_builtin_jobs(
    limit_per_agent: int = _BUILTIN_JOB_WORKER_BATCH_SIZE,
) -> dict[str, int]:
    """Submit fresh pending jobs to the persistent pool — DOES NOT wait.

    Returns telemetry describing what was scheduled this tick. Settlement
    happens asynchronously inside worker threads. Called once per worker
    tick; subsequent ticks see jobs claimed in earlier ticks transition out
    of the `pending` set via `claim_job`'s atomic state change, so nothing
    is double-claimed.
    """
    global _BUILTIN_WORKER_INFLIGHT_COUNT

    batch_limit = min(max(1, int(limit_per_agent)), 500)
    max_total = max(batch_limit, int(_BUILTIN_JOB_WORKER_MAX_BATCH_TOTAL))

    skill_agent_ids = set(_hosted_skills.list_pending_skill_agent_ids())
    eligible_agent_ids = set(_BUILTIN_AGENT_IDS) | skill_agent_ids

    # Reserve only the capacity that's actually free on the pool. Without
    # this, ticks under heavy load oversubscribe the pool — futures queue
    # internally and tick-level throughput becomes invisible because the
    # ThreadPoolExecutor's queue is unbounded.
    in_flight, capacity_remaining = _builtin_worker_inflight_snapshot()
    if capacity_remaining <= 0:
        return {
            "scanned": 0,
            "processed": 0,
            "queue_depth": 0,
            "in_flight": in_flight,
            "max_workers": int(_BUILTIN_JOB_WORKER_PARALLELISM),
            "configured_parallelism": int(_BUILTIN_JOB_WORKER_PARALLELISM),
            "saturated": True,
        }

    fetch_limit = min(max_total, capacity_remaining * 4)

    try:
        all_pending = jobs.list_pending_jobs(limit=fetch_limit)
    except AttributeError:
        all_pending = []
        for agent_id in eligible_agent_ids:
            all_pending.extend(
                jobs.list_jobs_for_agent(agent_id, status="pending", limit=batch_limit)
            )

    pending_jobs: list[dict] = []
    seen_ids: set[str] = set()
    per_agent_count: dict[str, int] = {}
    with _BUILTIN_WORKER_INFLIGHT_LOCK:
        already_inflight = set(_BUILTIN_WORKER_INFLIGHT_IDS)
    for job in all_pending:
        agent_id = str(job.get("agent_id") or "")
        if agent_id not in eligible_agent_ids:
            continue
        if per_agent_count.get(agent_id, 0) >= batch_limit:
            continue
        job_id = str(job.get("job_id") or "")
        if not job_id or job_id in seen_ids:
            continue
        # Defensive: an in-flight ID could still be visible in `pending` for a
        # microsecond before claim_job lands in the DB. Skip it explicitly.
        if job_id in already_inflight:
            continue
        seen_ids.add(job_id)
        per_agent_count[agent_id] = per_agent_count.get(agent_id, 0) + 1
        pending_jobs.append(job)
        if len(pending_jobs) >= capacity_remaining:
            break
    scanned = len(pending_jobs)

    if not pending_jobs:
        return {
            "scanned": 0,
            "processed": 0,
            "queue_depth": 0,
            "in_flight": in_flight,
            "max_workers": int(_BUILTIN_JOB_WORKER_PARALLELISM),
            "configured_parallelism": int(_BUILTIN_JOB_WORKER_PARALLELISM),
        }

    pool = _get_builtin_worker_pool()
    submitted = 0
    for job in pending_jobs:
        job_id = str(job.get("job_id") or "")
        with _BUILTIN_WORKER_INFLIGHT_LOCK:
            _BUILTIN_WORKER_INFLIGHT_COUNT += 1
            _BUILTIN_WORKER_INFLIGHT_IDS.add(job_id)

        def _runner(captured_job=job, captured_id=job_id) -> bool:
            global _BUILTIN_WORKER_INFLIGHT_COUNT
            try:
                return _process_pending_builtin_job(captured_job)
            except Exception:
                _LOG.exception("Built-in parallel worker task failed.")
                return False
            finally:
                with _BUILTIN_WORKER_INFLIGHT_LOCK:
                    _BUILTIN_WORKER_INFLIGHT_COUNT = max(
                        0, _BUILTIN_WORKER_INFLIGHT_COUNT - 1
                    )
                    _BUILTIN_WORKER_INFLIGHT_IDS.discard(captured_id)
                # When a slot frees up, wake the loop so the next batch starts
                # immediately rather than waiting up to interval_seconds. This
                # is what gets concurrent batches off the queue under load.
                _wake_event = globals().get("_BUILTIN_WORKER_WAKE_EVENT")
                if _wake_event is not None:
                    try:
                        _wake_event.set()
                    except Exception:
                        pass

        try:
            pool.submit(_runner)
            submitted += 1
        except Exception:
            with _BUILTIN_WORKER_INFLIGHT_LOCK:
                _BUILTIN_WORKER_INFLIGHT_COUNT = max(
                    0, _BUILTIN_WORKER_INFLIGHT_COUNT - 1
                )
                _BUILTIN_WORKER_INFLIGHT_IDS.discard(job_id)
            _LOG.exception("Failed to submit built-in worker task to pool.")

    try:
        remaining = jobs.count_pending_jobs(agent_ids=list(eligible_agent_ids))
    except Exception:
        remaining = None
    in_flight_after, _capacity_after = _builtin_worker_inflight_snapshot()
    return {
        "scanned": scanned,
        "processed": submitted,  # "submitted to pool" — settlement is async
        "queue_depth": remaining
        if remaining is not None
        else max(0, scanned - submitted),
        "in_flight": in_flight_after,
        "max_workers": int(_BUILTIN_JOB_WORKER_PARALLELISM),
        "configured_parallelism": int(_BUILTIN_JOB_WORKER_PARALLELISM),
    }


def _run_builtin_worker_rescue_async(reason: str) -> bool:
    """Start one non-blocking queue drain when the normal worker looks stale.

    Never run worker rescue inside a status HTTP request. A batch-status poll
    can happen while many slow jobs are queued, and blocking the request on
    those jobs caused empty-body 502s under Claude Code's concurrent polling.
    """
    global _BUILTIN_WORKER_RESCUE_RUNNING
    with _BUILTIN_WORKER_RESCUE_LOCK:
        if _BUILTIN_WORKER_RESCUE_RUNNING:
            return False
        _BUILTIN_WORKER_RESCUE_RUNNING = True

    def _target() -> None:
        global _BUILTIN_WORKER_RESCUE_RUNNING
        started = _utc_now_iso()
        try:
            summary = _process_pending_builtin_jobs(limit_per_agent=50)
            _set_builtin_worker_state(
                last_run_at=started,
                last_summary={**summary, "source": reason},
                last_error=None,
            )
        except Exception as exc:
            _LOG.exception("Built-in worker async rescue failed.")
            _set_builtin_worker_state(last_run_at=started, last_error=str(exc))
        finally:
            with _BUILTIN_WORKER_RESCUE_LOCK:
                _BUILTIN_WORKER_RESCUE_RUNNING = False

    threading.Thread(
        target=_target,
        name="aztea-builtin-rescue",
        daemon=True,
    ).start()
    return True


def _builtin_worker_loop(stop_event: threading.Event) -> None:
    worker_db_path = jobs.DB_PATH
    _set_builtin_worker_state(running=True, started_at=_utc_now_iso())
    interval = max(0.05, float(_BUILTIN_JOB_WORKER_INTERVAL_SECONDS))
    while not stop_event.is_set():
        # Wait up to `interval` seconds, but wake immediately when a new
        # batch is submitted (hire_batch / hire_async fire the wake event).
        woken = _BUILTIN_WORKER_WAKE_EVENT.wait(timeout=interval)
        if woken:
            _BUILTIN_WORKER_WAKE_EVENT.clear()
        if stop_event.is_set():
            break
        started = _utc_now_iso()
        try:
            if jobs.DB_PATH != worker_db_path:
                _set_builtin_worker_state(
                    last_run_at=started,
                    last_summary={"scanned": 0, "processed": 0},
                    last_error=None,
                )
                continue
            summary = _process_pending_builtin_jobs(
                limit_per_agent=_BUILTIN_JOB_WORKER_BATCH_SIZE
            )
            _set_builtin_worker_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
            # If anything ran AND there's still depth in the queue, drain
            # again immediately. This is what lets a 100+ job batch fan out
            # without waiting `interval` seconds between worker ticks.
            processed_this_tick = int(summary.get("processed", 0) or 0)
            queue_remaining = int(summary.get("queue_depth", 0) or 0)
            if processed_this_tick > 0 and queue_remaining > 0:
                _BUILTIN_WORKER_WAKE_EVENT.set()
            elif processed_this_tick >= _BUILTIN_JOB_WORKER_PARALLELISM:
                _BUILTIN_WORKER_WAKE_EVENT.set()
        except Exception as exc:
            _LOG.exception("Built-in worker loop failed.")
            _set_builtin_worker_state(
                last_run_at=started,
                last_error=str(exc),
            )
    _set_builtin_worker_state(running=False)


_TIE_TIMEOUT_HOURS = 48
_TIE_TIMEOUT_REASONING = (
    "Judges tied after two rounds. Finalizing in favor of the agent because "
    "the filer did not produce judge consensus."
)


def _run_pending_dispute_judgments(
    limit: int = 100, actor_owner_id: str = "system:dispute-judge"
) -> dict:
    capped = min(max(1, int(limit)), 500)
    # Pick up both 'pending' (never judged) and 'judging' (started but failed —
    # e.g., LLM exception left status at 'judging' with no recorded judgments).
    # Without this retry, a single transient LLM failure would strand disputes
    # forever (the eval-flagged P0 bug).
    pending = disputes.list_disputes(status="pending", limit=capped)
    pending += disputes.list_disputes(status="judging", limit=capped)
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
            latest, _ = _resolve_dispute_with_judges(
                dispute_id, actor_owner_id=actor_owner_id
            )
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

    # Auto-finalize stale tied disputes in favor of the agent. A caller-favoring
    # tie break creates positive expected value for frivolous disputes; the
    # filer bears the burden of producing judge consensus.
    stale_tied = disputes.get_stale_tied_disputes(
        older_than_hours=_TIE_TIMEOUT_HOURS, limit=capped
    )
    for dispute_row in stale_tied:
        dispute_id = str(dispute_row.get("dispute_id") or "").strip()
        if not dispute_id:
            continue
        try:
            disputes.record_judgment(
                dispute_id,
                judge_kind="human_admin",
                verdict="agent_wins",
                reasoning=_TIE_TIMEOUT_REASONING,
                model=None,
                admin_user_id="system_tie_timeout",
            )
            payments.post_dispute_settlement(
                dispute_id,
                outcome="agent_wins",
            )
            finalized = disputes.finalize_dispute(
                dispute_id,
                status="final",
                outcome="agent_wins",
            )
            if finalized is not None:
                _apply_dispute_effects(finalized, "agent_wins")
                job = jobs.get_job(finalized["job_id"])
                if job is not None:
                    _record_job_event(
                        job,
                        "job.dispute_finalized",
                        actor_owner_id=actor_owner_id,
                        payload={
                            "dispute_id": dispute_id,
                            "outcome": "agent_wins",
                            "reason": "tie_timeout",
                        },
                    )
            tie_timeout_count += 1
            _LOG.warning(
                "Tie-timeout auto-ruling: dispute %s -> agent_wins after %dh",
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
            summary = _run_pending_dispute_judgments(
                actor_owner_id="system:dispute-judge"
            )
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
            current = _validate_outbound_url(url, "healthcheck_url")
            import httpx as _httpx

            status = "unhealthy"
            for _ in range(4):
                resp = _httpx.get(current, timeout=10, follow_redirects=False)
                if 300 <= resp.status_code < 400 and resp.headers.get("location"):
                    current = _validate_outbound_url(
                        resp.headers["location"], "healthcheck_url"
                    )
                    continue
                status = "healthy" if 200 <= resp.status_code < 300 else "unhealthy"
                break
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


_ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "").strip()


def _send_reconciliation_drift_alert(summary: dict) -> None:
    """Email the ops address and capture to Sentry when ledger drift is detected."""
    drift = summary.get("drift_cents", "?")
    mismatch_count = summary.get("mismatch_count", "?")
    run_id = summary.get("run_id", "?")

    if _ALERT_EMAIL:
        subject = f"[Aztea] ALERT: Ledger drift detected — {drift}¢ across {mismatch_count} wallet(s)"
        html_body = (
            f"<p><strong>Ledger reconciliation detected drift.</strong></p>"
            f"<ul>"
            f"<li>Run ID: {run_id}</li>"
            f"<li>Total drift: {drift}¢</li>"
            f"<li>Wallets affected: {mismatch_count}</li>"
            f"</ul>"
            f"<p>Check server logs for <code>payments.reconciliation_invariant_failed</code> "
            f"and run <code>POST /ops/payments/reconcile</code> for full details.</p>"
        )
        text_body = (
            f"Ledger drift detected.\nRun ID: {run_id}\nDrift: {drift}¢\n"
            f"Wallets affected: {mismatch_count}\n\n"
            "Run POST /ops/payments/reconcile for full details."
        )
        _email.send(_ALERT_EMAIL, subject, html_body, text_body)

    # Surface in Sentry with structured extra context (not buried in logs).
    # push_scope() is the correct way to attach extra data to a single capture.
    if _SENTRY_DSN:
        try:
            import sentry_sdk

            with sentry_sdk.push_scope() as _scope:
                for _k, _v in summary.items():
                    _scope.set_extra(_k, _v)
                sentry_sdk.capture_message(
                    f"Ledger drift: {drift}¢ across {mismatch_count} wallet(s)",
                    level="error",
                )
        except Exception:
            _LOG.exception("Failed to capture reconciliation drift in Sentry.")


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
                _send_reconciliation_drift_alert(summary)
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

    placeholders = ",".join(["%s"] * len(owner_ids))
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
                INSERT INTO job_event_deliveries
                    (event_id, hook_id, owner_id, target_url, secret, payload,
                     status, attempt_count, next_attempt_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, %s, %s, %s)
                ON CONFLICT DO NOTHING
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
            INSERT INTO job_event_deliveries
                (event_id, hook_id, owner_id, target_url, secret, payload,
                 status, attempt_count, next_attempt_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, %s, %s, %s)
            ON CONFLICT DO NOTHING
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
    delay = _HOOK_DELIVERY_BASE_DELAY_SECONDS * (2**exponent)
    return min(delay, _HOOK_DELIVERY_MAX_DELAY_SECONDS)


def _claim_due_hook_delivery(now_iso: str) -> dict | None:
    from core import db as _db_kind

    with jobs._conn() as conn:
        try:
            if _db_kind.IS_POSTGRES:
                exists = conn.execute(
                    """
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_name = 'job_event_deliveries'
                    LIMIT 1
                    """
                ).fetchone()
            else:
                exists = conn.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'job_event_deliveries'
                    LIMIT 1
                    """
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                return None
            raise
        if exists is None:
            return None
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                return None
            raise
        row = conn.execute(
            """
            SELECT *
            FROM job_event_deliveries
            WHERE status = 'pending'
              AND next_attempt_at <= %s
            ORDER BY next_attempt_at ASC, delivery_id ASC
            LIMIT 1
            """,
            (now_iso,),
        ).fetchone()
        if row is None:
            return None

        claim_until_iso = (
            datetime.fromisoformat(now_iso)
            + timedelta(seconds=_HOOK_DELIVERY_CLAIM_LEASE_SECONDS)
        ).isoformat()
        result = conn.execute(
            """
            UPDATE job_event_deliveries
            SET next_attempt_at = %s,
                last_attempt_at = %s,
                updated_at = %s
            WHERE delivery_id = %s
              AND status = 'pending'
              AND next_attempt_at <= %s
            """,
            (claim_until_iso, now_iso, now_iso, row["delivery_id"], now_iso),
        )
        if result.rowcount == 0:
            return None

        claimed = conn.execute(
            "SELECT * FROM job_event_deliveries WHERE delivery_id = %s",
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
            SET last_attempt_at = %s,
                last_success_at = CASE WHEN %s = 1 THEN %s ELSE last_success_at END,
                last_status_code = %s,
                last_error = %s
            WHERE hook_id = %s
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
            SET status = %s,
                next_attempt_at = %s,
                attempt_count = COALESCE(%s, attempt_count),
                last_status_code = %s,
                last_error = %s,
                last_success_at = CASE WHEN %s = 1 THEN %s ELSE last_success_at END,
                updated_at = %s
            WHERE delivery_id = %s
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
                    "SELECT is_active FROM job_event_hooks WHERE hook_id = %s",
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
                status="failed"
                if (attempt_count + 1) >= _HOOK_DELIVERY_MAX_ATTEMPTS
                else "pending",
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
            digest = hmac.new(
                secret.encode("utf-8"), payload_bytes, hashlib.sha256
            ).hexdigest()
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
        next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(seconds=retry_delay)
        ).isoformat()
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
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]
