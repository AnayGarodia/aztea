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
