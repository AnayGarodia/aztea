from core import db as _db
# server.application shard 5 — verification, settlement, and dispute
# adjudication: output-verifier calls, registration verifier, quality-gate
# judge, dispute effects, cascaded child-job failure, dispute-window math,
# successful/failed settlement, judge resolution, reputation decay, endpoint
# health probes, and auto-suspension of low-performing agents. No HTTP routes.


def _list_job_events(
    caller: core_models.CallerContext, since: int | None = None, limit: int = 100
) -> list[dict]:
    limit = min(max(1, limit), 200)
    params: list[Any] = []
    where_clauses = []
    if caller["type"] != "master":
        where_clauses.append("(caller_owner_id = %s OR agent_owner_id = %s)")
        params.extend([caller["owner_id"], caller["owner_id"]])
    if since is not None:
        where_clauses.append("event_id > %s")
        params.append(since)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    params.append(limit)
    with jobs._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM job_events
            {where_sql}
            ORDER BY event_id ASC
            LIMIT %s
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
        _LOG.warning(
            "Agent registration verifier request failed for %s: %s",
            registration_payload.get("name"),
            exc,
        )
        return False, "registration verifier request failed"
    if not isinstance(body, dict):
        return False, "registration verifier returned non-object response"
    if bool(body.get("verified")):
        return True, str(body.get("reason") or "registration verifier passed")
    return False, str(
        body.get("reason") or "registration verifier returned verified=false"
    )


def _dq_fail(reason: str) -> dict:
    """Shorthand for a deterministic quality fail envelope."""
    return {"verdict": "fail", "score": 2, "reason": reason}


def _dq_pass(reason: str) -> dict:
    """Shorthand for a deterministic quality pass envelope."""
    return {"verdict": "pass", "score": 8, "reason": reason}


def _dq_required_keys(payload: dict, keys: list[str], agent_name: str) -> dict | None:
    """Return a fail envelope if any key is missing, else None."""
    for key in keys:
        if key not in payload:
            return _dq_fail(f"{agent_name} output must include '{key}'.")
    return None


def _dq_check_python_executor(payload: dict) -> dict:
    if not isinstance(payload.get("stdout"), str) or not isinstance(payload.get("stderr"), str):
        return _dq_fail("Python executor output must include stdout/stderr strings.")
    if not isinstance(payload.get("timed_out"), bool):
        return _dq_fail("Python executor output must include a boolean timed_out field.")
    if not isinstance(payload.get("variables_captured"), dict):
        return _dq_fail("Python executor output must include variables_captured.")
    try:
        int(payload.get("exit_code"))
        int(payload.get("execution_time_ms"))
    except (TypeError, ValueError):
        return _dq_fail("Python executor output must include numeric exit_code and execution_time_ms.")
    return _dq_pass("Structured Python executor output is internally consistent.")


def _dq_check_multi_language_executor(payload: dict) -> dict:
    if not isinstance(payload.get("stdout"), str) or not isinstance(payload.get("stderr"), str):
        return _dq_fail("Multi-language executor output must include stdout/stderr strings.")
    if not isinstance(payload.get("runtime"), str) or not str(payload.get("runtime")).strip():
        return _dq_fail("Multi-language executor output must include a runtime string.")
    passed = payload.get("passed")
    if not isinstance(passed, bool):
        return _dq_fail("Multi-language executor output must include a boolean passed field.")
    try:
        exit_code = int(payload.get("exit_code"))
        int(payload.get("execution_time_ms"))
    except (TypeError, ValueError):
        return _dq_fail("Multi-language executor output must include numeric exit_code and execution_time_ms.")
    if passed != (exit_code == 0):
        return _dq_fail("Multi-language executor output is internally inconsistent: passed does not match exit_code.")
    return _dq_pass("Structured multi-language executor output is internally consistent.")


def _dq_check_cve_lookup(payload: dict) -> dict:
    results = payload.get("results")
    if not isinstance(results, list):
        return _dq_fail("CVE lookup output must include a results list.")
    for item in results:
        if not isinstance(item, dict):
            return _dq_fail("Each CVE lookup result must be an object.")
        # Items returned from the direct cve_id fetch path use `cve_id`,
        # while the package-lookup path uses `cve`. Either is acceptable
        # — checking BOTH keeps deterministic-ID lookups from
        # non-deterministically failing the judge gate.
        cve_field = (
            str(item.get("cve") or "").strip() or str(item.get("cve_id") or "").strip()
        )
        error_field = str(item.get("error") or "").strip()
        # Items that include an error envelope (e.g. "not found", "NVD API
        # rate limit reached") are valid output shapes — the agent
        # successfully reported what it could not retrieve. Don't fail the
        # whole job for these.
        if not cve_field and not error_field:
            return _dq_fail("Each CVE lookup result must include a CVE identifier or an error field.")
        if cve_field:
            try:
                float(item.get("cvss", 0.0))
            except (TypeError, ValueError):
                return _dq_fail("Each CVE lookup result must include a numeric CVSS score.")
            if not isinstance(item.get("severity"), str):
                return _dq_fail("Each CVE lookup result must include a severity string.")
    return _dq_pass("Structured CVE lookup output is internally consistent.")


def _dq_check_secret_scanner(payload: dict) -> dict:
    findings = payload.get("findings")
    counts = payload.get("findings_by_severity")
    if not isinstance(findings, list) or not isinstance(counts, dict):
        return _dq_fail("Secret scanner output must include findings and findings_by_severity.")
    try:
        total_findings = int(payload.get("total_findings"))
    except (TypeError, ValueError):
        return _dq_fail("Secret scanner output must include a numeric total_findings.")
    if total_findings != len(findings):
        return _dq_fail("Secret scanner output is internally inconsistent: total_findings does not match findings.")
    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for item in findings:
        if not isinstance(item, dict):
            return _dq_fail("Each secret scanner finding must be an object.")
        severity = str(item.get("severity") or "").strip().lower()
        if severity not in severity_counts:
            return _dq_fail("Each secret scanner finding must include a valid severity.")
        severity_counts[severity] += 1
        if not isinstance(item.get("redacted_preview"), str):
            return _dq_fail("Each secret scanner finding must include a redacted_preview string.")
    if any(int(counts.get(key, 0)) != value for key, value in severity_counts.items()):
        return _dq_fail("Secret scanner output is internally inconsistent: findings_by_severity does not match findings.")
    return _dq_pass("Structured secret scanner output is internally consistent.")


def _dq_check_db_sandbox(payload: dict) -> dict:
    results = payload.get("results")
    if not isinstance(results, list):
        return _dq_fail("DB sandbox output must include a results list.")
    try:
        statements_executed = int(payload.get("statements_executed"))
        int(payload.get("db_size_bytes"))
        int(payload.get("execution_time_ms"))
    except (TypeError, ValueError):
        return _dq_fail("DB sandbox output must include numeric statements_executed, db_size_bytes, and execution_time_ms.")
    if statements_executed != len(results):
        return _dq_fail("DB sandbox output is internally inconsistent: statements_executed does not match results.")
    return _dq_pass("Structured DB sandbox output is internally consistent.")


def _dq_check_dns_inspector(payload: dict) -> dict:
    results = payload.get("results")
    if not isinstance(results, list):
        return _dq_fail("DNS inspector output must include a results list.")
    try:
        billing_units_actual = int(payload.get("billing_units_actual"))
    except (TypeError, ValueError):
        return _dq_fail("DNS inspector output must include numeric billing_units_actual.")
    successful_results = [
        item for item in results
        if isinstance(item, dict) and not item.get("error") and not item.get("issues")
    ]
    if billing_units_actual > len(results) or billing_units_actual < 0:
        return _dq_fail("DNS inspector output is internally inconsistent: billing_units_actual exceeds results.")
    if billing_units_actual not in {len(results), len(successful_results)}:
        return _dq_fail("DNS inspector output is internally inconsistent: billing_units_actual must count attempted domains or fully successful domains.")
    return _dq_pass("Structured DNS inspector output is internally consistent.")


def _dq_check_browser_agent(payload: dict) -> dict:
    if not isinstance(payload.get("url"), str) or not str(payload.get("url")).strip():
        return _dq_fail("Browser agent output must include a final URL.")
    if not isinstance(payload.get("html"), str) or not isinstance(payload.get("title"), str):
        return _dq_fail("Browser agent output must include html and title strings.")
    artifact = payload.get("screenshot_artifact")
    if not isinstance(artifact, dict) or not str(artifact.get("url_or_base64") or "").strip():
        return _dq_fail("Browser agent output must include a screenshot artifact.")
    return _dq_pass("Structured browser output is internally consistent.")


def _dq_check_docs_grounder(payload: dict) -> dict:
    if not isinstance(payload.get("sources"), list):
        return _dq_fail("Docs grounder output must include a sources list.")
    if not isinstance(payload.get("summary"), str):
        return _dq_fail("Docs grounder output must include a summary string.")
    if not str(payload.get("library") or "").strip():
        return _dq_fail("Docs grounder output must include a library field.")
    return _dq_pass("Docs grounder output is internally consistent.")


def _dq_check_sast_scanner(payload: dict) -> dict:
    findings = payload.get("findings")
    counts = payload.get("by_severity")
    if not isinstance(findings, list) or not isinstance(counts, dict):
        return _dq_fail("SAST scanner output must include findings and by_severity.")
    try:
        total = int(payload.get("total_findings"))
    except (TypeError, ValueError):
        return _dq_fail("SAST scanner output must include numeric total_findings.")
    if total != len(findings):
        return _dq_fail("SAST scanner output is inconsistent: total_findings does not match findings.")
    return _dq_pass("Structured SAST scanner output is internally consistent.")


def _dq_check_stripe_webhook_debugger(payload: dict) -> dict:
    results = payload.get("results")
    if not isinstance(results, list):
        return _dq_fail("Stripe webhook debugger output must include a results list.")
    try:
        tests_run = int(payload.get("tests_run"))
        passed = int(payload.get("passed"))
        failed = int(payload.get("failed"))
    except (TypeError, ValueError):
        return _dq_fail("Stripe webhook debugger output must include numeric tests_run, passed, failed.")
    if tests_run != passed + failed:
        return _dq_fail("Stripe webhook debugger output is inconsistent: tests_run != passed + failed.")
    return _dq_pass("Structured Stripe webhook debugger output is internally consistent.")


def _dq_check_load_tester(payload: dict) -> dict:
    latency = payload.get("latency_ms")
    if not isinstance(latency, dict):
        return _dq_fail("Load tester output must include a latency_ms dict.")
    try:
        total = int(payload.get("total_requests"))
        success = int(payload.get("success_count"))
        errors = int(payload.get("error_count"))
    except (TypeError, ValueError):
        return _dq_fail("Load tester output must include numeric total_requests, success_count, error_count.")
    if total != success + errors:
        return _dq_fail("Load tester output is inconsistent: total_requests != success_count + error_count.")
    return _dq_pass("Structured load tester output is internally consistent.")


def _dq_check_ci_failure_reproducer(payload: dict) -> dict:
    valid_types = {"code_error", "dependency_error", "env_error", "config_error", "flaky_test", "timeout", "unknown"}
    if str(payload.get("failure_type") or "") not in valid_types:
        return _dq_fail("CI failure reproducer output must include a valid failure_type.")
    if not isinstance(payload.get("commands_tried"), list):
        return _dq_fail("CI failure reproducer output must include commands_tried list.")
    return _dq_pass("Structured CI failure reproducer output is internally consistent.")


def _dq_check_ssl_certificate_decoder(payload: dict) -> dict:
    # Single cert returns subject/valid_from; batch returns certificates list
    has_single = "subject" in payload and "valid_from" in payload
    has_batch = "certificates" in payload
    if not has_single and not has_batch:
        return _dq_fail("SSL certificate decoder output must include subject+valid_from or certificates list.")
    return _dq_pass("Structured SSL certificate decoder output is internally consistent.")


def _dq_check_unicode_inspector(payload: dict) -> dict:
    has_single = "length_chars" in payload and "security" in payload
    has_batch = "results" in payload and "texts_analyzed" in payload
    if not has_single and not has_batch:
        return _dq_fail("Unicode inspector output must include length_chars+security or results+texts_analyzed.")
    return _dq_pass("Structured unicode inspector output is internally consistent.")


def _dq_check_color_contrast_checker(payload: dict) -> dict:
    has_single = "contrast_ratio" in payload and "grade" in payload
    has_batch = "results" in payload and "pairs_checked" in payload
    if not has_single and not has_batch:
        return _dq_fail("Color contrast checker output must include contrast_ratio+grade or results+pairs_checked.")
    return _dq_pass("Structured color contrast checker output is internally consistent.")


def _dq_check_keys_only(payload: dict, keys: list[str], agent_name: str) -> dict:
    """Validate that all required keys are present, return pass or fail."""
    miss = _dq_required_keys(payload, keys, agent_name)
    if miss is not None:
        return miss
    return _dq_pass(f"Structured {agent_name} output is internally consistent.")


# Dispatch table: agent_id -> pure checker(payload) -> dict.
# Evaluated lazily so agent ID constants are already bound at call time.
def _build_deterministic_quality_dispatch() -> dict:
    return {
        _PYTHON_EXECUTOR_AGENT_ID: _dq_check_python_executor,
        _MULTI_LANGUAGE_EXECUTOR_AGENT_ID: _dq_check_multi_language_executor,
        _CVELOOKUP_AGENT_ID: _dq_check_cve_lookup,
        _SECRET_SCANNER_AGENT_ID: _dq_check_secret_scanner,
        _DB_SANDBOX_AGENT_ID: _dq_check_db_sandbox,
        _DNS_INSPECTOR_AGENT_ID: _dq_check_dns_inspector,
        _BROWSER_AGENT_ID: _dq_check_browser_agent,
        _DOCS_GROUNDER_AGENT_ID: _dq_check_docs_grounder,
        _SAST_SCANNER_AGENT_ID: _dq_check_sast_scanner,
        _STRIPE_WEBHOOK_DEBUGGER_AGENT_ID: _dq_check_stripe_webhook_debugger,
        _LOAD_TESTER_AGENT_ID: _dq_check_load_tester,
        _CI_FAILURE_REPRODUCER_AGENT_ID: _dq_check_ci_failure_reproducer,
        _JWT_DEBUGGER_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["header", "payload", "algorithm", "decoded_at"], "JWT debugger"
        ),
        _DOCKERFILE_ANALYZER_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["findings", "total_findings", "by_severity", "score"], "Dockerfile analyzer"
        ),
        _OPENAPI_VALIDATOR_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["valid", "errors", "stats"], "OpenAPI validator"
        ),
        _COVERAGE_RUNNER_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["overall_pct", "exit_code", "files"], "Coverage runner"
        ),
        _EMAIL_DELIVERABILITY_CHECKER_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["domain", "spf", "dkim", "dmarc", "score", "verdict"], "Email deliverability checker"
        ),
        _REGEX_TESTER_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["results", "total_matches", "patterns_tested", "strings_tested"], "Regex tester"
        ),
        _CRON_EXPRESSION_PARSER_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["expression", "valid", "next_runs", "timezone"], "Cron expression parser"
        ),
        _SSL_CERTIFICATE_DECODER_AGENT_ID: _dq_check_ssl_certificate_decoder,
        _DIFF_ANALYZER_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["files_changed", "total_additions", "total_deletions", "risk_summary"], "Diff analyzer"
        ),
        _K8S_MANIFEST_VALIDATOR_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["valid", "resources_parsed", "total_findings", "by_severity"], "K8s manifest validator"
        ),
        _ARCHIVE_INSPECTOR_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["format", "total_entries", "total_uncompressed_bytes", "security"], "Archive inspector"
        ),
        _UNICODE_INSPECTOR_AGENT_ID: _dq_check_unicode_inspector,
        _TERRAFORM_PLAN_ANALYZER_AGENT_ID: lambda p: _dq_check_keys_only(
            p, ["summary", "changes", "risk_summary"], "Terraform plan analyzer"
        ),
        _COLOR_CONTRAST_CHECKER_AGENT_ID: _dq_check_color_contrast_checker,
    }


_DETERMINISTIC_QUALITY_DISPATCH: dict | None = None
_DETERMINISTIC_QUALITY_DISPATCH_LOCK = threading.Lock()


def _get_deterministic_quality_dispatch() -> dict:
    global _DETERMINISTIC_QUALITY_DISPATCH
    if _DETERMINISTIC_QUALITY_DISPATCH is not None:
        return _DETERMINISTIC_QUALITY_DISPATCH
    with _DETERMINISTIC_QUALITY_DISPATCH_LOCK:
        if _DETERMINISTIC_QUALITY_DISPATCH is None:
            _DETERMINISTIC_QUALITY_DISPATCH = _build_deterministic_quality_dispatch()
    return _DETERMINISTIC_QUALITY_DISPATCH


def _deterministic_quality_result(
    agent: dict, output_payload: dict
) -> dict[str, Any] | None:
    agent_id = str(agent.get("agent_id") or "").strip()
    payload = output_payload if isinstance(output_payload, dict) else {}
    checker = _get_deterministic_quality_dispatch().get(agent_id)
    if checker is None:
        return None
    return checker(payload)


def _quality_hint_for_agent(agent: dict) -> str:
    return ""


def _timeout_error_payload(job_payload: dict) -> dict:
    return error_codes.make_error(
        error_codes.AGENT_TIMEOUT,
        "Job lease expired before completion.",
        {"job": job_payload},
    )


def _create_judge_child_job(
    *,
    job: dict,
    agent: dict,
    output_payload: dict,
    judge_agent_id: str,
) -> str | None:
    """Create a zero-cost child job for the judge agent and return its job_id.

    Returns ``None`` on any error (judge job is optional; failure must not
    block the quality gate).
    """
    try:
        judge_agent = registry.get_agent(judge_agent_id)
        platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
        judge_wallet = payments.get_or_create_wallet(f"agent:{judge_agent_id}")
        child_charge_tx = payments.pre_call_charge(
            platform_wallet["wallet_id"], 0, judge_agent_id
        )
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
            client_id=job.get("client_id"),
            agent_owner_id=(judge_agent or {}).get("owner_id") or "master",
            max_attempts=1,
            parent_job_id=job["job_id"],
            parent_cascade_policy="detach",
            dispute_window_hours=1,
            judge_agent_id=None,
        )
        return child["job_id"]
    except Exception:
        return None


def _is_live_quality_enabled() -> bool:
    """Return True when the live LLM quality judge is toggled on and GROQ_API_KEY exists."""
    toggle = (
        os.environ.get("AZTEA_ENABLE_LIVE_QUALITY_JUDGE")
        or os.environ.get("AGENTMARKET_ENABLE_LIVE_QUALITY_JUDGE")
        or ""
    )
    return str(toggle).strip().lower() in {"1", "true", "yes", "on"} and bool(
        str(os.environ.get("GROQ_API_KEY", "")).strip()
    )


def _score_output_payload(
    *,
    job: dict,
    agent: dict,
    output_payload: dict,
    live_quality_enabled: bool,
) -> tuple[str, int, str, bool]:
    """Apply structural, schema, deterministic, and optionally live checks.

    Returns ``(verdict, score, reason, used_deterministic)``.
    Verdict is always 'pass' or 'fail'.
    """
    output_schema = agent.get("output_schema")
    verdict = "pass"
    score = 5
    reason = "No output contract defined. Structural check passed."
    parsed_output: Any
    try:
        parsed_output = json.loads(_stable_json_text(output_payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "fail", 0, "Output payload was not valid JSON.", False

    if parsed_output is None or parsed_output == {}:
        return "fail", 0, "Output payload must not be null or an empty object.", False

    if isinstance(output_schema, dict) and output_schema:
        schema_errors = _validate_json_schema_subset(parsed_output, output_schema)
        if schema_errors:
            return "fail", 0, f"Output did not match declared schema: {schema_errors[0]}", False
        reason = "Output matched declared schema and structural checks."

    deterministic = _deterministic_quality_result(agent, output_payload)
    if deterministic is not None:
        return (
            str(deterministic["verdict"]),
            int(deterministic["score"]),
            str(deterministic["reason"]),
            True,
        )

    if live_quality_enabled:
        try:
            judge_result = judges.run_quality_judgment(
                input_payload=job.get("input_payload") or {},
                output_payload=output_payload,
                agent_name=str(agent.get("name") or ""),
                agent_description=str(agent.get("description") or ""),
                quality_hint=_quality_hint_for_agent(agent),
            )
            judge_verdict = str(judge_result.get("verdict") or "").strip().lower()
            verdict = judge_verdict if judge_verdict in {"pass", "fail"} else "fail"
            try:
                score = max(0, min(10, int(judge_result.get("score"))))
            except (TypeError, ValueError):
                score = 1 if verdict == "fail" else 5
            reason = (
                str(judge_result.get("reason") or "").strip()
                or "Quality judge returned no reason."
            )
        except Exception as exc:
            return "fail", 0, f"quality judge error: {exc}", False

    return verdict, score, reason, False


def _finalize_judge_child_job(
    *,
    judge_job_id: str | None,
    verdict: str,
    score: int,
    reason: str,
) -> None:
    """Mark the judge child job complete and settled."""
    if judge_job_id is None:
        return
    child_output = {"verdict": verdict, "score": score, "reason": reason}
    child_complete = jobs.update_job_status(
        judge_job_id, "complete", output_payload=child_output, completed=True
    )
    if child_complete is not None:
        jobs.mark_settled(judge_job_id)


def _run_quality_gate(job: dict, agent: dict, output_payload: dict) -> dict[str, Any]:
    judge_agent_id = (
        str(job.get("judge_agent_id") or _QUALITY_JUDGE_AGENT_ID).strip()
        or _QUALITY_JUDGE_AGENT_ID
    )
    judge_job_id = _create_judge_child_job(
        job=job,
        agent=agent,
        output_payload=output_payload,
        judge_agent_id=judge_agent_id,
    )
    live_quality_enabled = _is_live_quality_enabled()
    verdict, score, reason, _used_det = _score_output_payload(
        job=job,
        agent=agent,
        output_payload=output_payload,
        live_quality_enabled=live_quality_enabled,
    )

    verifier_passed, verifier_reason = _run_output_verifier(
        agent.get("output_verifier_url"),
        job=job,
        output_payload=output_payload,
    )
    if verdict == "pass" and not verifier_passed:
        verdict = "fail"
        reason = f"{reason} External verifier: {verifier_reason}"

    _finalize_judge_child_job(
        judge_job_id=judge_job_id, verdict=verdict, score=score, reason=reason
    )

    return {
        "judge_agent_id": judge_agent_id,
        "judge_job_id": judge_job_id,
        "judge_verdict": verdict,
        "quality_score": score,
        "reason": reason,
        "passed": verdict == "pass",
        "verifier_reason": verifier_reason,
    }


def _apply_dispute_effects(dispute: dict, outcome: str) -> None:
    normalized_outcome = str(outcome or "").strip().lower()
    current_job = jobs.get_job(dispute["job_id"])
    was_settled = bool((current_job or {}).get("settled_at"))
    previous_outcome = (
        str((current_job or {}).get("dispute_outcome") or "").strip().lower()
    )
    job = jobs.set_job_dispute_outcome(dispute["job_id"], normalized_outcome)
    if job is None:
        return
    if not was_settled:
        jobs.mark_settled(dispute["job_id"])
        job = jobs.get_job(dispute["job_id"]) or job
    if normalized_outcome == "caller_wins" and previous_outcome != "caller_wins":
        registry.update_call_stats(
            job["agent_id"],
            latency_ms=0.0,
            success=False,
            price_cents=int(job.get("price_cents") or 0),
        )
    elif normalized_outcome in {"agent_wins", "split", "void"} and not was_settled:
        registry.update_call_stats(
            job["agent_id"],
            latency_ms=_job_latency_ms(job),
            success=True,
            price_cents=int(job.get("price_cents") or 0),
        )

    filed_by = str(dispute.get("filed_by_owner_id") or "").strip()
    if (
        filed_by.startswith("user:")
        and dispute.get("side") == "caller"
        and normalized_outcome == "agent_wins"
    ):
        payments.adjust_caller_trust_once(
            filed_by,
            delta=-0.05,
            reason="dispute_loss",
            related_id=dispute["dispute_id"],
        )


def _fail_open_jobs_for_agent(
    agent_id: str, actor_owner_id: str, reason: str
) -> dict[str, int]:
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
            settled = _settle_failed_job(
                updated,
                actor_owner_id=actor_owner_id,
                event_type="job.failed_agent_banned",
            )
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
    filing_deposit_cents = _compute_dispute_filing_deposit_cents(
        int(job.get("price_cents") or 0)
    )
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
    except _db.IntegrityError:
        conn.execute("ROLLBACK")
        existing = disputes.get_dispute_by_job(job["job_id"])
        if existing is not None:
            return existing
        raise HTTPException(
            status_code=409, detail="A dispute already exists for this job."
        )
    except ValueError as exc:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=403, detail=str(exc))
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
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        _LOG.exception(
            "Unexpected error opening output-rejection dispute for job %s",
            job.get("job_id"),
        )
        raise HTTPException(status_code=500, detail="Failed to open dispute.")


def _cascade_fail_active_child_jobs(
    parent_job: dict, actor_owner_id: str
) -> dict[str, Any]:
    active_children = jobs.list_child_jobs(
        parent_job["job_id"],
        statuses=("pending", "running", "awaiting_clarification"),
        limit=500,
    )
    failed_child_job_ids: list[str] = []
    for child in active_children:
        policy = (
            str(child.get("parent_cascade_policy") or "").strip().lower() or "detach"
        )
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


# Error codes from a builtin agent's run() that should NEVER bill the caller.
# Each builtin returns a structured envelope ``{"error": {"code": "...", ...}}``
# when its runtime dependency is missing or its config is incomplete. Treating
# these as successes was the highest-impact bug from the 2026-04-28 audit:
# Browser/Visual-Regression/Linter/Type-Checker/Image-Generator each charged
# the caller despite producing no useful output.
_AGENT_FAILURE_ERROR_CODE_SUFFIXES: tuple[str, ...] = (
    ".tool_unavailable",
    ".not_configured",
    ".missing_dependency",
    ".dependency_missing",
    ".invalid_input",
    ".invalid_payload",
    ".missing_input",
    ".missing_content",
    ".missing_code",
    ".missing_url",
    ".missing_language",
    ".invalid_language",
    ".invalid_min_entropy",
    ".unsupported_language",
    ".query_too_long",
    ".code_too_long",
    ".stdin_too_long",
    ".url_too_long",
    ".too_many_urls",
    ".url_blocked",
    ".unsafe_artifact",
    ".ambiguous_source",
    ".extraction_failed",
    ".clone_failed",
    ".all_fetches_failed",
    ".fetch_failed",
    ".timeout",
)
_AGENT_FAILURE_ERROR_CODE_EXACT: frozenset[str] = frozenset(
    {
        "request.invalid_input",
        "agent.tool_unavailable",
        "agent.not_configured",
    }
)


def _is_agent_failure_envelope(output: object) -> tuple[bool, str | None, str | None]:
    """Return ``(is_failure, error_code, message)`` for a builtin agent response.

    Built-in agents wrap dependency / configuration failures in a structured
    envelope of the form::

        {"error": {"code": "<agent>.tool_unavailable", "message": "..."}}

    or an inner ``{"output": {"error": {...}}}`` wrapper. We accept both, plus
    a top-level ``"error"`` string code, so the failure-on-error refund path
    works regardless of how an agent serialises its envelope.
    """
    if not isinstance(output, dict):
        return False, None, None
    candidate: object = output.get("error")
    # Some agents nest the result under "output" (matches the hosted-skill
    # contract); peek through one level when "error" is absent at the top.
    if candidate is None:
        inner = output.get("output")
        if isinstance(inner, dict) and isinstance(inner.get("error"), (dict, str)):
            candidate = inner.get("error")
    if isinstance(candidate, dict):
        code = str(candidate.get("code") or "").strip().lower()
        message = str(candidate.get("message") or "").strip()
    elif isinstance(candidate, str) and candidate.strip():
        code = candidate.strip().lower()
        message = ""
    else:
        return False, None, None
    if not code:
        return False, None, message or None
    if code in _AGENT_FAILURE_ERROR_CODE_EXACT:
        return True, code, message or None
    if any(code.endswith(suffix) for suffix in _AGENT_FAILURE_ERROR_CODE_SUFFIXES):
        return True, code, message or None
    # Any top-level structured error envelope from a built-in agent indicates
    # no usable output. Treat it as a failure even if the exact code is new.
    return True, code, message or None


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
    # Always respect explicit verification windows: if the job opted in
    # (window_seconds > 0) and the caller hasn't decided yet, hold settlement.
    if verification_status == "pending":
        return jobs.get_job(job["job_id"]) or job
    if verification_status == "rejected":
        return jobs.get_job(job["job_id"]) or job
    # The 72-hour implicit dispute window previously blocked ALL settlement,
    # including jobs where no verification was ever configured.  Default now:
    # settle immediately and rely on the dispute path for clawback.
    # Set AZTEA_REQUIRE_VERIFICATION=1 to restore the old gated behaviour.
    if _feature_flags.REQUIRE_VERIFICATION:
        # Explicit caller acceptance releases funds immediately; only implicit
        # acceptance paths are gated by the dispute window timeout.
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
            registry.update_call_stats(
                job["agent_id"],
                latency_ms=_job_latency_ms(job),
                success=True,
                price_cents=int(job.get("price_cents") or 0),
            )
    settled = jobs.get_job(job["job_id"]) or job
    if newly_settled:
        _record_job_event(
            settled,
            "job.settled",
            actor_owner_id=actor_owner_id,
            payload={
                "status": settled["status"],
                "settled_at": settled.get("settled_at"),
            },
        )
        try:
            agent_owner_id = settled.get("agent_owner_id", "")
            agent_email = _get_owner_email(agent_owner_id)
            if agent_email:
                _agent_row = registry.get_agent(settled.get("agent_id", ""))
                _agent_name = (_agent_row or {}).get("name", "agent")
                _owner_user = (
                    _auth.get_user_by_id(agent_owner_id.replace("user:", ""))
                    if agent_owner_id.startswith("user:")
                    else None
                )
                _owner_username = (_owner_user or {}).get("username", "there")
                distribution = payments.compute_success_distribution(
                    int(settled.get("price_cents") or 0),
                    platform_fee_pct=settled.get("platform_fee_pct_at_create"),
                    fee_bearer_policy=settled.get("fee_bearer_policy"),
                )
                payout_cents = int(distribution.get("agent_payout_cents") or 0)
                if payout_cents > 0:
                    _email.send_payout_received(
                        agent_email,
                        _owner_username,
                        payout_cents,
                        settled["job_id"],
                        _agent_name,
                    )
        except Exception as exc:
            _LOG.warning(
                "Failed to send payout email for job %s: %s", settled.get("job_id"), exc
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
            registry.update_call_stats(
                job["agent_id"],
                latency_ms=_job_latency_ms(job),
                success=False,
                price_cents=int(job.get("price_cents") or 0),
            )
    settled = jobs.get_job(job["job_id"]) or job
    if newly_settled:
        _record_job_event(
            settled,
            event_type,
            actor_owner_id=actor_owner_id,
            payload={
                "status": settled["status"],
                "error_message": settled.get("error_message"),
            },
        )
        try:
            caller_email = _get_owner_email(settled.get("caller_owner_id", ""))
            if caller_email:
                _agent_name = (registry.get_agent(settled["agent_id"]) or {}).get(
                    "name", settled["agent_id"]
                )
                _email.send_job_failed(
                    caller_email,
                    settled["job_id"],
                    _agent_name,
                    settled.get("error_message") or "",
                )
        except Exception as exc:
            _LOG.warning(
                "Failed to send job failure email for job %s: %s",
                settled.get("job_id"),
                exc,
            )
    if str(settled.get("status") or "").strip().lower() == "failed":
        _cascade_fail_active_child_jobs(settled, actor_owner_id=actor_owner_id)
    return settled


def _dispute_view(dispute_row: dict) -> dict:
    payload = dict(dispute_row)
    judgments = disputes.get_judgments(payload["dispute_id"])
    payload["judgments"] = judgments
    status = str(payload.get("status") or "").strip().lower()
    judges_completed = len(judgments)
    payload["judgments_required"] = 2
    payload["judges_completed"] = judges_completed
    payload["judgments_queued"] = max(0, 2 - judges_completed)
    filed_at = str(payload.get("filed_at") or "").strip()
    resolution_by = None
    next_judge_run_by = None
    try:
        filed_dt = datetime.fromisoformat(filed_at)
        if filed_dt.tzinfo is None:
            filed_dt = filed_dt.replace(tzinfo=timezone.utc)
        if status in {"pending", "judging"}:
            next_judge_run_by = (
                datetime.now(timezone.utc) + timedelta(seconds=60)
            ).isoformat()
            resolution_by = (filed_dt + timedelta(minutes=3)).isoformat()
        elif status == "tied":
            resolution_by = (filed_dt + timedelta(hours=48)).isoformat()
        elif status in {"resolved", "final"}:
            resolution_by = payload.get("resolved_at")
    except Exception:
        next_judge_run_by = None
    payload["next_judge_run_by"] = next_judge_run_by
    payload["resolution_by"] = resolution_by
    # Surface which LLM models have been assigned / have already weighed in.
    # Callers watching an in-flight dispute can see which judge(s) ran and
    # which model was used without having to decode the raw judgments list.
    judge_models_used = [
        str(j.get("model") or "unknown") for j in judgments if j.get("model")
    ]
    payload["judge_models_used"] = judge_models_used
    if status in {"pending", "judging"}:
        payload["eta_hint"] = (
            "Dispute judges run about once per minute. Two matching judgments "
            "resolve the dispute automatically."
        )
    elif status == "tied":
        payload["eta_hint"] = (
            "Tied disputes auto-finalize as agent_wins after 48 hours unless "
            "an admin resolves them first."
        )
    elif status in {"resolved", "final"}:
        payload["eta_hint"] = "Dispute is resolved."

    # Deposit disposition: every filer pays a small deposit when filing a
    # dispute. The eval flagged that the response showed `filing_deposit_cents`
    # but never said whether the filer's deposit was kept, refunded, or split.
    # Surface the actual disposition so callers don't have to walk the ledger:
    #   - pending/judging  → "held"
    #   - resolved + filer won (caller filed → caller_wins, agent filed → agent_wins)
    #         → "refunded_to_filer" (filer prevailed; deposit returned)
    #   - resolved + filer lost → "forfeit_to_judges_pool"
    #         (deposit funds the LLM judges + dissuades frivolous filings)
    #   - tied → "held_pending_admin" (admin tie-break may award either way)
    #   - split → "refunded_to_filer" (any meritorious claim gets the deposit back)
    deposit_cents = int(payload.get("filing_deposit_cents") or 0)
    side = str(payload.get("side") or "").strip().lower()
    outcome = str(payload.get("outcome") or "").strip().lower()
    if deposit_cents <= 0:
        deposit_disposition = "no_deposit_required"
    elif status in {"pending", "judging"}:
        deposit_disposition = "held"
    elif status == "tied":
        deposit_disposition = "held_pending_admin"
    elif status in {"resolved", "final"}:
        if outcome == "split":
            deposit_disposition = "refunded_to_filer"
        elif (side == "caller" and outcome == "caller_wins") or (
            side == "agent" and outcome == "agent_wins"
        ):
            deposit_disposition = "refunded_to_filer"
        elif outcome in {"caller_wins", "agent_wins"}:
            deposit_disposition = "forfeit_to_judges_pool"
        elif outcome == "void":
            deposit_disposition = "refunded_to_filer"
        else:
            deposit_disposition = "unknown"
    else:
        deposit_disposition = "unknown"

    payload["filing_deposit_disposition"] = deposit_disposition
    payload["filing_deposit_explanation"] = {
        "no_deposit_required": "No deposit was required for this dispute.",
        "held": (
            "Filer's deposit is held in escrow until LLM judges resolve the "
            "dispute. POLICY: deposit is REFUNDED in full if the filer "
            "prevails (judges rule in your favor, dispute is voided, or "
            "outcome is split); deposit is FORFEIT to the judges' compute "
            "pool if the filer does not prevail. Forfeit funds the judges "
            "and dissuades frivolous filings."
        ),
        "held_pending_admin": (
            "Judges tied. Deposit is held until an admin resolves the dispute "
            "or the 48-hour auto-finalize window elapses. Same policy applies: "
            "refunded if filer prevails, forfeit otherwise."
        ),
        "refunded_to_filer": "The filer's deposit was returned because they prevailed (or the dispute was voided/split).",
        "forfeit_to_judges_pool": "The filer's deposit was forfeit because they did not prevail. The deposit funds the LLM judges' compute and dissuades frivolous filings.",
        "unknown": "Deposit disposition is indeterminate for this dispute state.",
    }[deposit_disposition]
    payload["filing_deposit_policy"] = {
        "on_filer_prevails": "refunded_to_filer",
        "on_filer_loses": "forfeit_to_judges_pool",
        "on_split_or_void": "refunded_to_filer",
        "on_tied_judges": "held_pending_admin (admin tie-break or 48h auto-finalize)",
    }
    return payload


def _dispute_side_for_caller(caller: core_models.CallerContext, job: dict) -> str:
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Master key cannot file disputes.")
    owner_id = caller["owner_id"]
    if owner_id == job["caller_owner_id"]:
        return "caller"
    if _caller_worker_authorized_for_job(caller, job):
        return "agent"
    raise HTTPException(
        status_code=403, detail="Only the caller or agent owner can file this dispute."
    )


def _resolve_dispute_with_judges(
    dispute_id: str, actor_owner_id: str
) -> tuple[dict, dict | None]:
    result = judges.run_judgment(dispute_id)
    status = str(result.get("status") or "").strip().lower()
    outcome = result.get("outcome")
    settlement = None

    if status == "consensus" and outcome:
        dispute_row = disputes.get_dispute(dispute_id)
        if dispute_row is None:
            raise HTTPException(
                status_code=404, detail=f"Dispute '{dispute_id}' not found."
            )
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
        raise HTTPException(
            status_code=404, detail=f"Dispute '{dispute_id}' not found."
        )
    job = jobs.get_job(latest["job_id"])
    if job is not None:
        _record_job_event(
            job,
            "job.dispute_judged",
            actor_owner_id=actor_owner_id,
            payload={
                "dispute_id": dispute_id,
                "status": latest["status"],
                "outcome": latest.get("outcome"),
            },
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
        reference = _parse_iso_datetime(
            row["last_completed_at"]
        ) or _parse_iso_datetime(row["created_at"])
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
        current_multiplier = max(
            0.0, min(1.0, float(row["trust_decay_multiplier"] or 1.0))
        )
        new_multiplier = current_multiplier * (
            (1.0 - _REPUTATION_DECAY_DAILY_RATE) ** elapsed_days
        )
        new_multiplier = max(0.0, min(1.0, new_multiplier))
        if new_multiplier >= current_multiplier:
            continue
        registry.set_agent_decay_multiplier(
            row["agent_id"], new_multiplier, current.isoformat()
        )
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


def _probe_agent_endpoint_health(
    endpoint_url: str, timeout_seconds: int
) -> tuple[bool, str | None]:
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
        previous_status = (
            str(agent.get("endpoint_health_status") or "unknown").strip().lower()
        )
        previous_failures = _to_non_negative_int(
            agent.get("endpoint_consecutive_failures"), default=0
        )
        endpoint_url = str(agent.get("endpoint_url") or "").strip()
        ok = False
        error_text: str | None = None
        try:
            ok, error_text = _probe_agent_endpoint_health(
                endpoint_url, timeout_seconds=timeout_seconds
            )
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
    # Curated built-in agents are NEVER auto-suspended. They run on
    # in-process internal endpoints; their failure-rate is dominated by
    # caller-side bad inputs (test red-team payloads, schema fuzzing in
    # eval runs, intentionally invalid CVE IDs, etc.) — none of which
    # reflect agent health. The 2026-05-09 eval surfaced this when
    # Browser Agent / Visual Regression / Shell Executor / Live Endpoint
    # Tester all hit the >60% failure threshold and silently disappeared
    # from search results, leaving the rails looking degraded for hours
    # until a manual UPDATE.
    curated_ids = _builtin_constants.CURATED_PUBLIC_BUILTIN_AGENT_IDS
    with jobs._conn() as conn:
        if curated_ids:
            placeholders = ",".join(["%s"] * len(curated_ids))
            rows = conn.execute(
                f"""
                SELECT agent_id, owner_id, successful_calls, total_calls
                FROM agents
                WHERE status = 'active'
                  AND total_calls >= %s
                  AND agent_id NOT IN ({placeholders})
                """,
                (AUTO_SUSPEND_MIN_CALLS, *curated_ids),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT agent_id, owner_id, successful_calls, total_calls
                FROM agents
                WHERE status = 'active' AND total_calls >= %s
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
                "UPDATE agents SET status = 'suspended' WHERE agent_id = %s AND status = 'active'",
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
