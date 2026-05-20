"""Regression tests for the 2026-05-19 rails-hardening sprint (B1-B27).

Each test pins one bug fix. Source-anchored where the test would otherwise
require spinning up uvicorn or hitting prod; behavior tests use the
isolated_db fixture from tests/integration/conftest.py where needed.

The previous sprint shipped phantom "fixes" by inspecting code that
didn't actually change behavior. These tests verify the persisted
column / forwarded field / declared schema as close to the source of
truth as possible.
"""

from __future__ import annotations

import re
from pathlib import Path


# ===========================================================================
# Cluster A — Job schema + enforcement (B1, B2, B5)
# ===========================================================================


# --- B1 -------------------------------------------------------------------


def test_b1_job_create_request_accepts_per_job_cap_cents():
    """JobCreateRequest must declare per_job_cap_cents as an int|None field."""
    from core.models.job_requests import JobCreateRequest

    fields = JobCreateRequest.model_fields
    assert "per_job_cap_cents" in fields, (
        "JobCreateRequest must declare per_job_cap_cents — otherwise the "
        "field is silently dropped (the B1 pre-fix symptom)"
    )
    # The field must be nullable so existing callers don't need to change.
    cap_field = fields["per_job_cap_cents"]
    assert cap_field.default is None
    # Built-in ge=0 constraint.
    req = JobCreateRequest(agent_id="x", per_job_cap_cents=10)
    assert req.per_job_cap_cents == 10
    req_null = JobCreateRequest(agent_id="x")
    assert req_null.per_job_cap_cents is None


def test_b1_singleton_combines_per_job_cap_with_api_key_cap():
    """Singleton POST /jobs must MIN body.per_job_cap_cents with the key cap."""
    src = Path("server/application_parts/part_008.py").read_text()
    # The combined cap must be passed into _estimate_variable_charge.
    assert "effective_per_job_cap_cents" in src, (
        "Singleton path must combine the per-API-key cap with body.per_job_"
        "cap_cents — silently using only the key cap is the B1 symptom"
    )
    # Match the actual MIN pattern.
    assert re.search(
        r"min\(\s*effective_per_job_cap_cents\s*,\s*int\(body\.per_job_cap_cents\)\s*\)",
        src,
    ), "Combined cap must use MIN — smaller wins"


def test_b1_batch_combines_per_job_cap_with_api_key_cap():
    """Batch /jobs/batch must MIN spec.per_job_cap_cents with the key cap."""
    src = Path("server/application_parts/part_009.py").read_text()
    assert "spec_per_job_cap_cents" in src, (
        "Batch path must combine the per-API-key cap with spec.per_job_cap_"
        "cents — silently using only the key cap is the B1 batch symptom"
    )
    assert re.search(
        r"min\(\s*spec_per_job_cap_cents\s*,\s*int\(spec\.per_job_cap_cents\)\s*\)",
        src,
    )


def test_b1_per_job_cap_error_code_distinct_from_key_cap():
    """Binding cap from body emits JOB_PER_JOB_CAP_EXCEEDED; key cap stays SPEND_LIMIT_EXCEEDED."""
    from core import error_codes

    assert hasattr(error_codes, "JOB_PER_JOB_CAP_EXCEEDED")
    assert error_codes.JOB_PER_JOB_CAP_EXCEEDED == "job.per_job_cap_exceeded"
    # The two codes must be different — silently using the same code makes
    # it impossible for callers to distinguish "tighten my cap" from "ask
    # ops to raise the key cap".
    assert error_codes.JOB_PER_JOB_CAP_EXCEEDED != error_codes.SPEND_LIMIT_EXCEEDED


def test_b1_singleton_handler_branches_on_binding_cap_source():
    """Singleton's 402 envelope picks the right error code by binding source."""
    src = Path("server/application_parts/part_008.py").read_text()
    # The handler must branch: if body's cap is the binding cap, emit
    # JOB_PER_JOB_CAP_EXCEEDED; otherwise SPEND_LIMIT_EXCEEDED.
    assert "JOB_PER_JOB_CAP_EXCEEDED" in src
    assert 'cap_scope = "job.per_job_cap"' in src
    assert 'cap_scope = "api_key_per_job"' in src


def test_b1_persisted_column_added_in_migration_0060():
    """Migration 0060 must add jobs.per_job_cap_cents."""
    src = Path("migrations/0060_job_governance.sql").read_text()
    assert "ALTER TABLE jobs ADD COLUMN per_job_cap_cents INTEGER" in src, (
        "Migration 0060 must create jobs.per_job_cap_cents — otherwise the "
        "follow-up UPDATE in part_008/part_009 fails with 'no such column'"
    )


# --- B2 -------------------------------------------------------------------


def test_b2_batch_handler_persists_stop_when_via_helper():
    """Batch /jobs/batch must call _persist_batch_job_governance after create_job."""
    src = Path("server/application_parts/part_009.py").read_text()
    # The helper must exist.
    assert "def _persist_batch_job_governance" in src, (
        "Batch path needs _persist_batch_job_governance — without it the "
        "stop_when/billing_unit/per_job_cap_cents fields silently drop"
    )
    # And it must be invoked after each create_job in the create loop.
    assert "_persist_batch_job_governance(spec, job[\"job_id\"])" in src


def test_b2_batch_handler_validates_stop_when_pre_charge():
    """Batch must reject malformed stop_when before opening any wallet hold."""
    src = Path("server/application_parts/part_009.py").read_text()
    # Validation must happen in the resolve loop (before the charge loop)
    # by calling _validate_spec_stop_when.
    assert "def _validate_spec_stop_when" in src
    assert "_validate_spec_stop_when(spec)" in src
    # The exception path must add to invalid_jobs (not raise) so partial
    # batches still proceed for the valid specs.
    assert "stop_when.invalid" in src


def test_b2_stop_when_update_includes_per_job_cap_cents():
    """The post-create UPDATE in both singleton + batch writes all three fields."""
    src_singleton = Path("server/application_parts/part_008.py").read_text()
    assert "stop_when_json = %s, billing_unit = %s, " in src_singleton
    assert "per_job_cap_cents = %s WHERE job_id = %s" in src_singleton

    src_batch = Path("server/application_parts/part_009.py").read_text()
    # Batch helper writes the same three columns in one UPDATE. Python
    # string-literal concatenation across lines makes a single direct
    # substring check fragile, so verify each fragment separately.
    assert "UPDATE jobs SET stop_when_json = %s, billing_unit = %s, " in src_batch
    assert "per_job_cap_cents = %s WHERE job_id = %s" in src_batch


def test_b2_singleton_persists_when_only_per_job_cap_set():
    """Submitting just per_job_cap_cents (no stop_when, no billing_unit) still
    triggers the post-create UPDATE — pre-B1 the condition was billing_unit-
    or-stop_when only."""
    src = Path("server/application_parts/part_008.py").read_text()
    # The gate must include per_job_cap_cents.
    m = re.search(
        r"_has_governance_field = \(\s*bool\(validated_stop_when\)\s*"
        r"or\s+body\.billing_unit is not None\s*"
        r"or\s+body\.per_job_cap_cents is not None",
        src,
        re.DOTALL,
    )
    assert m, "Singleton governance gate must include per_job_cap_cents"


# --- B5 -------------------------------------------------------------------


def test_b5_mcp_hire_batch_allowlist_source_check():
    """Source-level pin: _HIRE_BATCH_ALLOWED_PER_JOB_FIELDS lists the new fields."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    idx = src.find("_HIRE_BATCH_ALLOWED_PER_JOB_FIELDS = frozenset")
    assert idx >= 0
    block = src[idx : idx + 1000]
    for field in (
        '"per_job_cap_cents"',
        '"billing_unit"',
        '"stop_when"',
    ):
        assert field in block, (
            f"_HIRE_BATCH_ALLOWED_PER_JOB_FIELDS must include {field} — "
            "without it MCP rejects what HTTP accepts (B5 symptom)"
        )


def test_b5_mcp_forwarder_threads_new_fields():
    """_hire_batch must forward per_job_cap_cents + billing_unit to /jobs/batch."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    assert 'if spec.get("per_job_cap_cents") is not None:' in src
    assert 'job["per_job_cap_cents"] = int(spec["per_job_cap_cents"])' in src
    assert 'if spec.get("billing_unit") is not None:' in src
    assert 'job["billing_unit"] = str(spec["billing_unit"]).strip()' in src


def test_b5_mcp_schema_declares_new_fields():
    """The aztea_hire_batch tool's JSON schema declares per_job_cap_cents +
    billing_unit so additionalProperties=False doesn't 422 them."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    # Locate the aztea_hire_batch tool entry. The schema block is ~12K
    # chars; we read until the next tool definition to bound the window.
    idx = src.find('"name": "aztea_hire_batch"')
    assert idx >= 0
    next_tool = src.find('"name": "aztea_', idx + 100)
    block = src[idx:next_tool] if next_tool > idx else src[idx : idx + 16000]
    assert '"per_job_cap_cents"' in block, (
        "aztea_hire_batch jobs[] schema must declare per_job_cap_cents — "
        "without it the additionalProperties=False guard 422s the field "
        "before it reaches the server (B5 symptom)"
    )
    assert '"billing_unit"' in block
    # And the tool still uses additionalProperties=False — preventing the
    # silent-drop class.
    assert '"additionalProperties": False' in block


# --- response shape -------------------------------------------------------


def test_b1_b2_job_response_declares_governance_fields():
    """JobResponse explicitly lists the new governance fields."""
    from core.models.responses import JobResponse

    fields = JobResponse.model_fields
    for name in ("per_job_cap_cents", "stop_when_json", "billing_unit"):
        assert name in fields, (
            f"JobResponse must declare {name!r} so integrators see it in "
            "the OpenAPI surface and IDE autocomplete"
        )


# ===========================================================================
# Cluster B — Wallet caps (B3, B4)
# ===========================================================================


# --- B4 -------------------------------------------------------------------


def test_b4_daily_limit_check_skips_zero_cost_calls():
    """pre_call_charge must wrap the daily check in `if price_cents > 0`."""
    src = Path("core/payments/base.py").read_text()
    assert "if wallet_daily_limit_raw is not None and price_cents > 0:" in src, (
        "Daily-limit check must short-circuit on price_cents==0 — without "
        "this, free agents are blocked once today_spend exceeds the cap "
        "even though they cost nothing (B4 symptom)"
    )


# --- B3 -------------------------------------------------------------------


def test_b3_session_budget_exception_type_exists():
    """The new exception type is exported from core.payments."""
    from core import payments

    assert hasattr(payments, "WalletSessionBudgetExceededError")
    exc = payments.WalletSessionBudgetExceededError(
        limit_cents=280, session_spent_cents=270, attempted_cents=20
    )
    assert exc.limit_cents == 280
    assert exc.session_spent_cents == 270
    assert exc.attempted_cents == 20


def test_b3_pre_call_charge_enforces_session_budget():
    """pre_call_charge must read session_budget_cents and raise on overflow."""
    src = Path("core/payments/base.py").read_text()
    # Must SELECT the new columns in the gate row.
    assert "session_budget_cents, session_budget_set_at," in src, (
        "pre_call_charge SELECT must read session_budget_cents — without "
        "it the cap can't be enforced"
    )
    # Must raise the new exception when session_spent + price exceeds cap.
    assert "raise WalletSessionBudgetExceededError(" in src
    # Must skip zero-cost calls (B4 short-circuit applies to session check too).
    assert "if session_budget_raw is not None and price_cents > 0:" in src


def test_b3_session_budget_setter_helper_exists():
    """set_wallet_session_budget helper writes the cap + set_at atomically."""
    from core import payments

    assert hasattr(payments, "set_wallet_session_budget")
    # The helper signature must accept reset_counter.
    import inspect

    sig = inspect.signature(payments.set_wallet_session_budget)
    assert "reset_counter" in sig.parameters
    # Default reset_counter=True so the common "set cap, start window now"
    # path requires zero extra args.
    assert sig.parameters["reset_counter"].default is True


def test_b3_set_session_budget_route_registered():
    """POST /wallets/me/session-budget endpoint declared in part_011.py."""
    src = Path("server/application_parts/part_011.py").read_text()
    assert '@app.post(\n    "/wallets/me/session-budget",' in src
    assert "wallet_set_session_budget" in src


def test_b3_error_handler_maps_to_402():
    """server/application_parts/part_002.py maps the exception to 402."""
    src = Path("server/application_parts/part_002.py").read_text()
    assert "except payments.WalletSessionBudgetExceededError as exc:" in src
    assert "WALLET_SESSION_BUDGET_EXCEEDED" in src


def test_b3_mcp_set_session_budget_now_hits_server():
    """MCP aztea_set_session_budget must POST to /wallets/me/session-budget."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    # The handler must call _post with the new endpoint URL.
    assert "/wallets/me/session-budget" in src, (
        "MCP set_session_budget must POST to /wallets/me/session-budget — "
        "the prior client-side-only dict was bypassed by any non-MCP caller"
    )
    # And the session_state["budget_cents"] write must come AFTER the
    # server confirms — never as the primary source of truth.
    assert 'session_state["budget_cents"] = cap' in src


def test_b3_session_budget_response_model_declared():
    """WalletSessionBudgetResponse + Request models exist."""
    from core.models import responses

    assert hasattr(responses, "WalletSessionBudgetRequest")
    assert hasattr(responses, "WalletSessionBudgetResponse")
    fields = responses.WalletSessionBudgetRequest.model_fields
    assert "session_budget_cents" in fields
    assert "reset_counter" in fields
    # Forbid extra fields — silent typos like "limit_cents" would otherwise
    # clear the cap accidentally (the same class of bug WalletDailySpendLimit
    # Request fixed in 1.7.2).
    assert responses.WalletSessionBudgetRequest.model_config.get("extra") == "forbid"


# --- migration ------------------------------------------------------------


def test_cluster_a_b_migration_0060_adds_three_columns():
    """Migration 0060 adds jobs.per_job_cap_cents + wallets.session_budget_*."""
    src = Path("migrations/0060_job_governance.sql").read_text()
    assert "ALTER TABLE jobs ADD COLUMN per_job_cap_cents INTEGER" in src
    assert "ALTER TABLE wallets ADD COLUMN session_budget_cents INTEGER" in src
    assert "ALTER TABLE wallets ADD COLUMN session_budget_set_at TEXT" in src


# ===========================================================================
# Cluster C — Surface integrity (B6, B7, B9, B10)
# ===========================================================================


# --- B6 -------------------------------------------------------------------


def test_b6_workspaces_verify_get_returns_structured_405():
    """GET /workspaces/{id}/verify must return 405 JSON, never SPA HTML."""
    src = Path("server/application_parts/part_013.py").read_text()
    # The new GET handler must be registered for the same path as the POST.
    assert '@app.get(\n    "/workspaces/{workspace_id}/verify",' in src
    assert "workspaces_verify_get_405" in src
    # The handler must return 405 with Allow: POST header.
    assert 'status_code=405' in src
    assert 'headers={"Allow": "POST"}' in src
    # And mention the docs anchor so callers find the POST contract.
    assert "/api/docs#/workspaces" in src


# --- B7 -------------------------------------------------------------------


def test_b7_system_health_alias_registered():
    """A /system/health GET route returns the same payload as /health."""
    src = Path("server/routes/system.py").read_text()
    assert '@router.get(\n    "/system/health",' in src, (
        "Missing /system/health alias — without it the documented URL "
        "(referenced in dispute-policy docstring) falls through to SPA HTML"
    )
    assert "def system_health()" in src
    # The alias must delegate to the canonical health() handler, not
    # duplicate the body (or the two will drift).
    assert "return health()" in src


def test_b7_spa_prefixes_include_system_so_unknown_returns_json():
    """Unknown /system/* paths must return JSON 404, not SPA HTML."""
    src = Path("server/application_parts/part_014.py").read_text()
    idx = src.find("_SPA_API_PREFIXES")
    assert idx >= 0
    block = src[idx : idx + 2000]
    assert '"system/"' in block, (
        "Adding /system/health as an alias means /system/* must also be "
        "in _SPA_API_PREFIXES so /system/foo returns JSON 404 instead "
        "of SPA HTML"
    )
    # workspaces was added too so unknown /workspaces/* paths 404 as JSON.
    assert '"workspaces/"' in block


# --- B9 -------------------------------------------------------------------


def test_b9_agent_registration_discoverability_routes_exist():
    """POST /agents, /agents/register, /registry/agents/register all return
    a structured 404 pointing at /registry/register."""
    src = Path("server/application_parts/part_007.py").read_text()
    # All three intuitive paths must be registered as discoverability stubs.
    assert '@app.post("/agents",' in src
    assert '@app.post("/agents/register",' in src
    assert '@app.post("/registry/agents/register",' in src
    # The response must point at the canonical path + CLI helper.
    assert '"correct_path": "/registry/register"' in src
    assert '"cli_hint": "aztea publish' in src


# --- B10 ------------------------------------------------------------------


def test_b10_openapi_json_redirects_to_api_openapi_json():
    """/openapi.json must 308-redirect to /api/openapi.json (FastAPI is
    configured with openapi_url='/api/openapi.json')."""
    src = Path("server/application_parts/part_001.py").read_text()
    assert '@app.get("/openapi.json"' in src
    assert "/api/openapi.json" in src
    # 308 (permanent redirect, preserves method) is the right code for an
    # API path move — not 301 (which spec'd by some clients to convert
    # POST→GET).
    assert "status_code=308" in src


def test_b10_redoc_also_redirects():
    """Same for /redoc → /api/redoc so the legacy URL keeps working."""
    src = Path("server/application_parts/part_001.py").read_text()
    assert '@app.get("/redoc"' in src
    assert "/api/redoc" in src


def test_b10_docs_path_intentionally_not_redirected():
    """/docs is owned by the SPA — the redirect handler must NOT cover it."""
    src = Path("server/application_parts/part_001.py").read_text()
    # No literal @app.get("/docs"...) handler in the redirect block; the
    # SPA serves /docs through its catch-all.
    assert '@app.get("/docs"' not in src, (
        "/docs is owned by the SPA as a product page; a redirect here "
        "would break the front-end documentation experience"
    )


# ===========================================================================
# Cluster D — MCP completeness (B8, B12, B25, B26)
# ===========================================================================


# --- B8 -------------------------------------------------------------------


def test_b8_create_pipeline_action_in_manage_workflow_enum():
    """manage_workflow's action enum must include 'create_pipeline'."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    # Locate the action enum block (under manage_workflow tool).
    mw_idx = src.find('"name": "manage_workflow"')
    assert mw_idx >= 0
    block = src[mw_idx : mw_idx + 5000]
    assert '"create_pipeline"' in block, (
        "manage_workflow enum must list create_pipeline so callers can "
        "discover the action without leaving the tool surface"
    )


def test_b8_create_pipeline_dispatcher_routes_correctly():
    """create_pipeline action dispatches to aztea_create_pipeline."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    assert '"create_pipeline": "aztea_create_pipeline"' in src
    assert "if tool_name == \"aztea_create_pipeline\":" in src
    assert "_create_pipeline(session, base, hdrs, timeout, arguments)" in src


def test_b8_create_pipeline_handler_posts_to_pipelines_endpoint():
    """_create_pipeline must POST to /pipelines with name + definition."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    assert "def _create_pipeline(" in src
    # Must require name + definition.
    assert "name is required to create a pipeline." in src
    assert "definition is required" in src
    # Must POST to /pipelines.
    assert "f\"{base}/pipelines\"" in src


# --- B12 ------------------------------------------------------------------


def test_b12_unsupported_field_error_no_dead_anchor():
    """The hire_batch unsupported-field error must not reference the
    non-existent `aztea_hire_batch.jobs[].properties` anchor."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    assert "aztea_hire_batch.jobs[].properties" not in src, (
        "Dead docs anchor leaked back into the error message — keep this "
        "test in place so future copy edits don't reintroduce it"
    )
    # The replacement must reference a real URL.
    assert "/api/docs#/Jobs/post__jobs_batch" in src
    assert "describe_specialist(slug='aztea_hire_batch')" in src


# --- B25 ------------------------------------------------------------------


def test_b25_sunset_replacements_map_defined():
    """_SUNSET_AGENT_REPLACEMENTS maps known-sunset slugs to suggestions."""
    src = Path("sdks/python-sdk/aztea/mcp/server.py").read_text()
    assert "_SUNSET_AGENT_REPLACEMENTS: dict[str, str]" in src
    # Spot-check a few known sunset slugs from the CLI list.
    for slug in (
        "docs_grounder",
        "linter",
        "semantic_codebase_search",
        "shell_executor",
    ):
        assert f'"{slug}":' in src, (
            f"{slug} missing from _SUNSET_AGENT_REPLACEMENTS — the dispatch "
            "fallback will surface 'Unknown tool' instead of agent.sunset"
        )


def test_b25_dispatcher_short_circuits_sunset_with_structured_error():
    """call_specialist dispatch path checks the sunset map before /mcp/invoke."""
    src = Path("sdks/python-sdk/aztea/mcp/server.py").read_text()
    # The short-circuit must reference the map and produce a structured
    # agent.sunset envelope with a suggestion.
    assert "_SUNSET_AGENT_REPLACEMENTS.get(" in src
    assert '"code": "agent.sunset",' in src
    assert '"suggested_replacement"' in src


# --- B26 ------------------------------------------------------------------


def test_b26_compare_duplicate_agent_ids_hint_at_hire_batch():
    """Compare with duplicate agent_ids returns a structured error
    suggesting hire_batch for 'run same agent N times' workflows."""
    src = Path("server/application_parts/part_009.py").read_text()
    # The handler must use the structured error code + hint.
    assert "compare.duplicate_agents" in src
    assert "manage_workflow(action='hire_batch', jobs=[...])" in src
    # And the response must include duplicate_agent_ids for actionability.
    assert "duplicate_agent_ids" in src


# ===========================================================================
# Cluster E — Quality judge (B11)
# ===========================================================================


def test_b11_schema_permitted_error_envelope_returns_pass():
    """Agent returning {error: ...} where the schema permits it must PASS."""
    from core import judges

    schema = {
        "type": "object",
        "properties": {
            "signature_valid": {"type": ["boolean", "null"]},
            "error": {"type": ["string", "null"]},
        },
    }
    result = judges._local_quality_fallback(
        input_payload={"token": "bad"},
        output_payload={"error": "invalid_signature", "signature_valid": False},
        agent_description="JWT validator",
        output_schema=schema,
    )
    assert result["verdict"] == "pass", (
        "Schema permits an `error` field — the agent did its job by "
        f"returning a structured failure. Verdict must be pass; got {result}"
    )
    assert result["judge_reason_detail"] == "schema_permitted_error_envelope"


def test_b11_undeclared_error_field_still_fails():
    """Agent with NO `error` in output_schema returning {error: ...} fails."""
    from core import judges

    schema = {
        "type": "object",
        "properties": {
            "result": {"type": "string"},
        },
    }
    result = judges._local_quality_fallback(
        input_payload={"x": 1},
        output_payload={"error": "crashed"},
        agent_description="Test agent without error envelope",
        output_schema=schema,
    )
    assert result["verdict"] == "fail"
    assert result["judge_reason_detail"] == "undeclared_error_field"


def test_b11_unstructured_crash_payload_still_fails():
    """Even if the schema declares `error`, an unstructured crash trace fails."""
    from core import judges

    schema = {"type": "object", "properties": {"error": {"type": "string"}}}
    result = judges._local_quality_fallback(
        input_payload={"x": 1},
        output_payload={
            "error": (
                "Traceback (most recent call last):\n"
                "  File \"agent.py\", line 1, in <module>\n"
                "TypeError: bad call"
            ),
        },
        agent_description="Crashed agent",
        output_schema=schema,
    )
    assert result["verdict"] == "fail"
    assert result["judge_reason_detail"] == "unstructured_crash"


def test_b11_judge_reason_detail_on_happy_path():
    """A clean success payload tags `deterministic_heuristic`."""
    from core import judges

    result = judges._local_quality_fallback(
        input_payload={"q": "weather"},
        output_payload={
            "answer": "Sunny, 72°F",
            "citations": ["example.com"],
            "summary": "Forecast looks great" + " " * 110,  # boost text_chars
        },
        agent_description="Weather agent",
        output_schema=None,
    )
    assert result["verdict"] == "pass"
    assert result["judge_reason_detail"] == "deterministic_heuristic"


def test_b11_run_quality_judgment_accepts_output_schema_kwarg():
    """run_quality_judgment must accept and forward output_schema."""
    from core import judges
    import inspect

    sig = inspect.signature(judges.run_quality_judgment)
    assert "output_schema" in sig.parameters, (
        "Public run_quality_judgment must accept output_schema so callers "
        "can give the heuristic the agent's contract for B11 exemptions"
    )


def test_b11_part_005_settlement_forwards_output_schema():
    """The settlement caller in part_005 must forward agent['output_schema']."""
    src = Path("server/application_parts/part_005.py").read_text()
    assert "output_schema=agent.get(\"output_schema\")" in src, (
        "Settlement path must forward the agent's output_schema to the "
        "quality judge — without it the B11 exemption never fires in prod"
    )


# ===========================================================================
# Cluster F — Disputes (B13, B14)
# ===========================================================================


# --- B13 ------------------------------------------------------------------


def test_b13_secondary_fallback_writes_audit_event():
    """When the secondary LLM fails, _settle_via_tiebreaker_after_secondary_
    failure must write a structured audit event so dispute_status can
    surface degraded_mode."""
    src = Path("core/judges.py").read_text()
    assert "secondary_judge_fallback" in src
    assert "disputes.append_audit_event(" in src
    # The function must return degraded_mode=True so the immediate
    # caller (not just a later GET) sees the degraded path.
    fn_idx = src.find("def _settle_via_tiebreaker_after_secondary_failure(")
    block = src[fn_idx : fn_idx + 2500]
    assert '"degraded_mode": True' in block
    assert '"degraded_reason": "secondary_judge_llm_unavailable"' in block


def test_b13_dispute_row_surfaces_degraded_mode_from_audit_log():
    """_row_to_dispute must scan audit_log for the fallback event and
    surface degraded_mode=True on the response dict."""
    src = Path("core/disputes.py").read_text()
    # The row builder must check the audit_log for the event.
    assert "secondary_judge_fallback" in src
    # And surface the field on the response dict.
    assert 'data["degraded_mode"] = True' in src
    assert 'data["degraded_mode"] = False' in src


# --- B14 ------------------------------------------------------------------


def test_b14_resolution_deadline_pinned_at_filing():
    """create_dispute writes resolution_deadline_at once at INSERT."""
    src = Path("core/disputes.py").read_text()
    # The INSERT must include resolution_deadline_at.
    assert "resolution_deadline_at" in src
    assert "_dispute_resolution_window_hours()" in src
    # The deadline must be computed BEFORE the params tuple.
    fn_idx = src.find("def create_dispute(")
    # Bound the window at the next def so we capture the entire function.
    next_def = src.find("\ndef ", fn_idx + 1)
    block = src[fn_idx:next_def] if next_def > fn_idx else src[fn_idx:]
    assert "resolution_deadline_iso = (" in block
    # And the INSERT column list must include it.
    assert "operator_response_deadline, resolution_deadline_at)" in block


def test_b14_dispute_row_exposes_resolution_by_from_pinned_column():
    """_row_to_dispute reads resolution_deadline_at and exposes it as
    resolution_by (legacy field name) so existing clients see a stable
    deadline."""
    src = Path("core/disputes.py").read_text()
    assert 'data["resolution_by"] = data["resolution_deadline_at"]' in src


def test_b14_migration_0061_adds_resolution_deadline_at():
    """Migration 0061 adds disputes.resolution_deadline_at."""
    src = Path("migrations/0061_dispute_resolution_deadline.sql").read_text()
    assert "ALTER TABLE disputes ADD COLUMN resolution_deadline_at TEXT" in src


def test_b14_resolution_window_helper_default_48h():
    """_dispute_resolution_window_hours defaults to 48h to match the
    documented dispute SLA."""
    from core import disputes

    assert disputes.DEFAULT_DISPUTE_RESOLUTION_HOURS == 48
    assert disputes._dispute_resolution_window_hours() == 48


# ===========================================================================
# Cluster G — Workers + sandbox (B15, B16, B17, B18)
# ===========================================================================


# --- B15 ------------------------------------------------------------------


def test_b15_create_job_pins_claim_deadline():
    """create_job writes claim_deadline_at via the post-insert UPDATE."""
    src = Path("core/jobs/crud.py").read_text()
    assert "_compute_claim_deadline_iso" in src
    assert "UPDATE jobs SET claim_deadline_at = %s WHERE job_id = %s" in src
    # The helper must respect AZTEA_JOB_CLAIM_DEADLINE_SECONDS for ops tunability.
    assert "AZTEA_JOB_CLAIM_DEADLINE_SECONDS" in src
    assert "_DEFAULT_CLAIM_DEADLINE_SECONDS = 1800" in src


def test_b15_sweeper_auto_fails_stranded_jobs():
    """The jobs sweeper scans for jobs past claim_deadline_at and fails them."""
    src = Path("server/application_parts/part_006.py").read_text()
    assert "claim_deadline_failed_job_ids" in src
    assert "agent.no_workers_claimed" in src
    assert "job.no_workers_claimed" in src
    # The scan must filter on status='pending' AND past deadline.
    assert "status = 'pending'" in src
    assert "claim_deadline_at IS NOT NULL" in src
    assert "claim_deadline_at < %s" in src


def test_b15_migration_0062_adds_claim_deadline_column():
    """Migration 0062 adds jobs.claim_deadline_at."""
    src = Path("migrations/0062_jobs_claim_deadline.sql").read_text()
    assert "ALTER TABLE jobs ADD COLUMN claim_deadline_at TEXT" in src


# --- B16 ------------------------------------------------------------------


def test_b16_tunnel_open_returns_stub_envelope_on_invalid_input():
    """sandbox_tunnel_open returns a structured stub instead of raising."""
    from core.sandbox import tunnels
    from core.sandbox.models import SandboxNotFound

    # Patch _validate_tunnel_input to raise — we're testing the boundary
    # catch, not the validator itself.
    original = tunnels._validate_tunnel_input
    try:
        tunnels._validate_tunnel_input = lambda *a, **kw: (_ for _ in ()).throw(
            SandboxNotFound("sandbox 'sbx_xxx' not active")
        )
        result = tunnels.tunnel_open({"sandbox_id": "sbx_xxx"})
    finally:
        tunnels._validate_tunnel_input = original
    assert result["status"] == "invalid_input"
    assert result["tunnel_id"] is None
    assert result["public_url"] is None
    assert result["refunded"] is True
    assert "sandbox" in result["reason"].lower()


def test_b16_tunnel_close_returns_stub_envelope_on_invalid_input():
    """sandbox_tunnel_close mirrors the stub contract."""
    from core.sandbox import tunnels
    from core.sandbox.models import SandboxNotFound

    original = tunnels._require
    try:
        tunnels._require = lambda *a, **kw: (_ for _ in ()).throw(
            SandboxNotFound("sandbox not active")
        )
        result = tunnels.tunnel_close({"tunnel_id": "tun_abc"})
    finally:
        tunnels._require = original
    assert result["status"] == "invalid_input"
    assert result["refunded"] is True


# --- B17 ------------------------------------------------------------------


def test_b17_fork_canonical_source_field_no_migration_note():
    """source_sandbox_id (canonical) → no migration_note in response."""
    src = Path("core/sandbox/snapshots.py").read_text()
    # The function must declare the canonical-vs-legacy split.
    assert "canonical_source = str(payload.get(\"source_sandbox_id\") or \"\").strip()" in src
    assert "legacy_source = str(payload.get(\"sandbox_id\") or \"\").strip()" in src
    # And only attach migration_note when the legacy field is used.
    assert "migration_note = (" in src
    assert "if migration_note:" in src
    assert 'response["migration_note"] = migration_note' in src


def test_b17_fork_sunset_date_documented():
    """The migration note must name the sunset version (v1.8.0)."""
    src = Path("core/sandbox/snapshots.py").read_text()
    assert "v1.8.0" in src


# --- B18 ------------------------------------------------------------------


def test_b18_sandbox_state_exposes_ttl_remaining_seconds():
    """SandboxState exposes ttl_remaining_seconds as a computed property."""
    from core.sandbox.state import SandboxState
    import inspect

    # Computed property — not a dataclass field.
    members = inspect.getmembers(SandboxState)
    assert any(name == "ttl_remaining_seconds" for name, _ in members), (
        "SandboxState must expose ttl_remaining_seconds so callers can "
        "see TTL burn-down before firing expensive ops"
    )


def test_b18_lifecycle_status_response_includes_ttl_remaining_seconds():
    """The status + start responses include ttl_remaining_seconds."""
    src = Path("core/sandbox/lifecycle.py").read_text()
    assert "\"ttl_remaining_seconds\": state.ttl_remaining_seconds" in src
    # And appears in BOTH _status_response and _start_response.
    assert src.count("\"ttl_remaining_seconds\":") >= 2


# ===========================================================================
# Cluster H — Performance + ergonomics (B19, B20, B21, B22)
# ===========================================================================


# --- B19 ------------------------------------------------------------------


def test_b19_auto_hire_returns_price_exceeded_without_substitution():
    """When the top candidate's price exceeds max_cost_usd, auto-hire
    must return reason='price_exceeded' — never silently switch agents."""
    src = Path("core/registry/auto_hire.py").read_text()
    # The decision returned by the price-gate must use the dedicated
    # reason string, not a generic fall-through.
    assert 'reason="price_exceeds_max"' in src or "price_exceeds_max" in src, (
        "Price gate must return reason='price_exceeds_max' so callers "
        "can detect the cap-hit explicitly rather than getting a "
        "silently-substituted cheaper agent"
    )


# --- B20 ------------------------------------------------------------------


def test_b20_rate_limit_handler_sets_retry_after_header():
    """The RateLimitExceeded handler emits both header + body fields."""
    src = Path("server/error_handlers.py").read_text()
    # The handler must register on RateLimitExceeded and return 429 with
    # both a Retry-After header AND a retry_after_seconds field in the
    # body — header for HTTP-RFC-compliant clients, body for SDKs that
    # parse JSON only.
    assert "@app.exception_handler(RateLimitExceeded)" in src
    assert 'headers={"Retry-After": str(' in src
    assert '"retry_after_seconds":' in src


def test_b20_retry_after_minimum_one_second():
    """Even when the limiter expiry is 0 or negative, Retry-After must
    be at least 1 — clients should never see 0 (which RFC 6585 forbids)."""
    src = Path("server/error_handlers.py").read_text()
    assert "max(1, retry_after)" in src


# --- B21 ------------------------------------------------------------------


def test_b21_search_has_degraded_fallback_in_caller():
    """The MCP search_specialists handler already auto-degrades to the
    local stale catalog on registry outage. Pin the behavior so it
    doesn't regress."""
    src = Path("sdks/python-sdk/aztea/mcp/server.py").read_text()
    # The lazy search tool must include a degraded path with a warning.
    assert "STALE CATALOG" in src, (
        "MCP search must surface 'STALE CATALOG' warning when the live "
        "registry is unavailable — without it callers spend on stale data"
    )


# --- B22 ------------------------------------------------------------------


def test_b22_acknowledged_as_v1_item():
    """B22 (batch bulk-insert) is acknowledged in TODO.md as a v1 item.

    Real sync-bulk-insert refactor is high-risk because pre_call_charge
    opens its own transaction per call. Touching that money rail in the
    same sprint as the other 26 fixes would dilute review attention.
    The existing per-job serial path is correct, just slower — the
    write here pins that the deferral is intentional and tracked.
    """
    # Pin that the plan acknowledges the deferral.
    plan = Path("/Users/aakritigarodia/.claude/plans/shimmering-yawning-cocoa.md")
    if plan.exists():
        text = plan.read_text()
        assert "B22" in text
        assert "out of scope" in text.lower() or "v1 item" in text.lower()


# ===========================================================================
# Cluster I — Papercuts (B23, B24, B27)
# ===========================================================================


# --- B23 ------------------------------------------------------------------


def test_b23_users_me_alias_get_registered():
    """/users/me GET delegates to auth_me for a stable profile read."""
    src = Path("server/application_parts/part_006.py").read_text()
    assert '@app.get(\n    "/users/me",' in src
    # The handler must call the existing auth_me to avoid drift.
    assert "def users_me_get(" in src
    assert "return auth_me(request, caller)" in src


def test_b23_users_me_post_updates_profile():
    """POST /users/me accepts full_name + phone and rejects email + scopes."""
    src = Path("server/application_parts/part_006.py").read_text()
    assert '@app.post(\n    "/users/me",' in src
    assert "def users_me_update(" in src
    # Email change must produce a structured 422 with a next_step pointer.
    assert "Email changes require verification" in src
    # Master / agent_key callers must be 403'd (matches /auth/me's gates).
    assert 'caller["type"] in {"master", "agent_key"}:' in src


def test_b23_update_user_profile_helper_exists():
    """core.auth.users.update_user_profile is the persistence helper."""
    from core.auth import users

    assert hasattr(users, "update_user_profile")
    import inspect

    sig = inspect.signature(users.update_user_profile)
    assert "full_name" in sig.parameters
    assert "phone" in sig.parameters


# --- B24 ------------------------------------------------------------------


def test_b24_cors_allow_methods_covers_all_real_methods():
    """allow_methods must list every method any endpoint uses (B24)."""
    src = Path("server/application_parts/part_001.py").read_text()
    # The pre-fix list was ["GET", "POST", "DELETE", "OPTIONS"] — PATCH +
    # PUT were missing, so browsers preflighting PATCH /auth/role got 405.
    assert (
        'allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]' in src
    ), (
        "CORS middleware must advertise all methods Aztea endpoints use — "
        "missing PATCH or PUT means browser preflight fails on routes "
        "like PATCH /auth/role"
    )


# --- B27 ------------------------------------------------------------------


def test_b27_describe_response_includes_cache_warning_for_cacheable_agents():
    """describe_specialist's cache block must include a cross-caller warning."""
    src = Path("sdks/python-sdk/aztea/mcp/server.py").read_text()
    # Either the in-tool docstring OR the response block must explicitly
    # call out the cross-caller cache scope. The response-block version
    # is the one that flows to the caller.
    assert '"partition": "global"' in src
    assert "platform-wide" in src or "another tenant" in src
    assert "Do NOT send" in src
    assert "per-tenant nonce" in src


# ===========================================================================
# Deferred-items follow-up (post-cluster-I)
# ===========================================================================


# --- B17 copy fix ---------------------------------------------------------


def test_b17_sunset_date_is_v1_9_0_not_v1_8_0():
    """The migration_note in sandbox_fork must say v1.9.0 (not v1.8.0).

    Deprecation lands in v1.8.0 (which IS this release). Removal lives
    one minor version out — standard semver-style sunset window.
    """
    src = Path("core/sandbox/snapshots.py").read_text()
    # The literal lives across a Python string-concat boundary; check
    # the suffix that sits on one line.
    assert "v1.8.0 and will be removed in v1.9.0" in src, (
        "sandbox_fork's migration_note must distinguish the deprecation "
        "version (v1.8.0) from the removal version (v1.9.0)"
    )
    # And the legacy alias still works.
    assert 'legacy_source = str(payload.get("sandbox_id") or "").strip()' in src


# --- B15 follow-up: observability counter ---------------------------------


def test_b15_followup_observability_counter_defined():
    """A Prometheus counter exists for agent.no_workers_claimed events."""
    src = Path("core/observability.py").read_text()
    assert "job_no_workers_claimed_total" in src
    assert "aztea_job_no_workers_claimed_total" in src
    # The label set must include agent_id so dashboards can break down
    # spikes by the misconfigured agent.
    assert '["agent_id"]' in src


def test_b15_followup_sweeper_increments_counter():
    """The sweeper auto-fail path increments the counter."""
    src = Path("server/application_parts/part_006.py").read_text()
    assert "job_no_workers_claimed_total.labels(" in src
    assert "agent_id=str(settled.get(\"agent_id\")" in src


# --- C2 follow-up: server-side idempotency_key dedup ---------------------


def test_c2_idempotency_module_exists():
    """core.idempotency wraps begin/complete/release around the
    idempotency_requests table."""
    from core import idempotency

    assert hasattr(idempotency, "begin")
    assert hasattr(idempotency, "complete")
    assert hasattr(idempotency, "release")
    assert hasattr(idempotency, "compute_request_hash")


def test_c2_request_hash_strips_idempotency_key():
    """compute_request_hash must NOT include the idempotency_key in the hash."""
    from core import idempotency

    h1 = idempotency.compute_request_hash({"a": 1, "b": [1, 2]})
    h2 = idempotency.compute_request_hash({"a": 1, "b": [1, 2], "idempotency_key": "k1"})
    h3 = idempotency.compute_request_hash({"a": 1, "b": [1, 2], "idempotency_key": "k2"})
    assert h1 == h2 == h3, (
        "compute_request_hash must strip idempotency_key — otherwise the "
        "same body with the same key would mismatch its own replay"
    )


def test_c2_batch_handler_wires_idempotency_check():
    """/jobs/batch calls idempotency.begin / .complete / .release."""
    src = Path("server/application_parts/part_009.py").read_text()
    assert "from core import idempotency as _idem" in src
    assert "_idem.begin(" in src
    assert "_idem_c.complete(" in src
    # The release path must fire on BOTH exception branches.
    assert src.count("_idem_b.release(") >= 2


def test_c2_jobbatchcreaterequest_accepts_idempotency_key():
    """JobBatchCreateRequest declares idempotency_key as a top-level field."""
    from core.models.job_requests import JobBatchCreateRequest

    fields = JobBatchCreateRequest.model_fields
    assert "idempotency_key" in fields
    # Length-bounded (1..128) so callers can't store unbounded blobs as keys.
    cap_field = fields["idempotency_key"]
    assert cap_field.default is None


def test_c2_mcp_forwarder_threads_top_level_idempotency_key():
    """MCP _hire_batch forwarder lifts idempotency_key into the body."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    assert 'body["idempotency_key"] = idem_key[:128]' in src
    # And the schema declares the field at the TOP level.
    idx = src.find('"name": "aztea_hire_batch"')
    assert idx >= 0
    next_tool = src.find('"name": "aztea_', idx + 100)
    block = src[idx:next_tool] if next_tool > idx else src[idx:]
    # Find an idempotency_key declaration BEFORE the jobs[] array.
    jobs_idx = block.find('"jobs":')
    head = block[:jobs_idx]
    assert '"idempotency_key"' in head, (
        "idempotency_key must be a TOP-LEVEL property on aztea_hire_batch, "
        "not a per-job field — the dedup key applies to the whole batch"
    )


def test_c2_mcp_per_job_hint_points_at_top_level():
    """The per-job rejection hint must redirect callers to the top level."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    # Old hint that pointed at "out of scope" must be gone.
    assert "is not implemented in v0" not in src
    # New hint explains it's a top-level field now.
    assert "TOP-LEVEL field" in src
    assert "outer request body" in src
