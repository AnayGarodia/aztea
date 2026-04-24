"""Server integration tests (auto-split fragment 4/6)."""

from tests.integration.support import *  # noqa: F403

def test_hook_delete_cancels_pending_deliveries(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    hook_resp = client.post(
        "/ops/jobs/hooks",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"target_url": "https://hooks.example.com/cancel-me"},
    )
    assert hook_resp.status_code == 201, hook_resp.text
    hook_id = hook_resp.json()["hook_id"]

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Cancel Hook Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["hooks-cancel"],
    )
    _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)

    deleted = client.delete(
        f"/ops/jobs/hooks/{hook_id}",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert deleted.status_code == 200, deleted.text

    with jobs._conn() as conn:
        counts = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM job_event_deliveries
            WHERE hook_id = ?
            GROUP BY status
            """,
            (hook_id,),
        ).fetchall()
    by_status = {row["status"]: int(row["count"]) for row in counts}
    assert by_status.get("pending", 0) == 0
    assert by_status.get("cancelled", 0) >= 1


def test_sweeper_auto_suspends_poor_agent_performance(client):
    worker = _register_user()
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Auto Suspend Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["auto-suspend"],
    )
    for _ in range(7):
        registry.update_call_stats(agent_id, latency_ms=50.0, success=False)
    for _ in range(3):
        registry.update_call_stats(agent_id, latency_ms=50.0, success=True)

    summary = server._sweep_jobs(limit=10, actor_owner_id="system:test-sweeper")
    assert summary["auto_suspended_count"] >= 1
    assert agent_id in summary["auto_suspended_agent_ids"]
    assert registry.get_agent(agent_id)["status"] == "suspended"

    server._set_sweeper_state(last_summary=summary)
    metrics = client.get("/ops/jobs/metrics", headers=_auth_headers(TEST_MASTER_KEY))
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["auto_suspended_last_sweep"] >= 1

    events = client.get("/ops/jobs/events", headers=_auth_headers(TEST_MASTER_KEY))
    assert events.status_code == 200, events.text
    assert any(
        event.get("event_type") == "agent_auto_suspended" and event.get("agent_id") == agent_id
        for event in events.json()["events"]
    )


def test_quality_gate_fails_schema_mismatch_without_live_judge(monkeypatch):
    monkeypatch.delenv("AZTEA_ENABLE_LIVE_QUALITY_JUDGE", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    def should_not_run_judge(**kwargs):
        raise AssertionError("Live quality judge should not run for schema mismatch.")

    monkeypatch.setattr(server.judges, "run_quality_judgment", should_not_run_judge)

    result = server._run_quality_gate(
        {"job_id": "job-schema-fail", "agent_id": "agent-x", "input_payload": {"task": "x"}},
        {
            "description": "schema-enforced agent",
            "output_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
            "output_verifier_url": None,
        },
        {"wrong": "shape"},
    )
    assert result["judge_verdict"] == "fail"
    assert result["quality_score"] == 0
    assert result["passed"] is False
    assert "Output did not match declared schema" in result["reason"]


def test_quality_gate_honest_fallback_without_contract_or_judge(monkeypatch):
    monkeypatch.delenv("AZTEA_ENABLE_LIVE_QUALITY_JUDGE", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    def should_not_run_judge(**kwargs):
        raise AssertionError("Live quality judge should not run in fallback path.")

    monkeypatch.setattr(server.judges, "run_quality_judgment", should_not_run_judge)

    result = server._run_quality_gate(
        {"job_id": "job-fallback", "agent_id": "agent-y", "input_payload": {"task": "x"}},
        {
            "description": "no contract agent",
            "output_schema": None,
            "output_verifier_url": None,
        },
        {"result": "ok"},
    )
    assert result["judge_verdict"] == "pass"
    assert result["quality_score"] == 5
    assert result["passed"] is True
    assert result["reason"] == "No output contract defined. Structural check passed."


def test_builtin_worker_auto_completes_async_jobs(client, monkeypatch):
    monkeypatch.setattr(
        server.agent_python_executor,
        "run",
        lambda payload: {
            "stdout": "processed by test builtin worker\n",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "execution_time_ms": 10,
            "explanation": "processed by test builtin worker",
            "variables_captured": {},
        },
    )

    master_wallet = payments.get_or_create_wallet("master")
    payments.deposit(master_wallet["wallet_id"], 500, "test builtin funds")

    created = client.post(
        "/jobs",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={
            "agent_id": server._PYTHON_EXECUTOR_AGENT_ID,
            "input_payload": {"code": "print('hello')"},
            "max_attempts": 2,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]

    terminal = None
    for _ in range(24):
        state = client.get(f"/jobs/{job_id}", headers=_auth_headers(TEST_MASTER_KEY))
        assert state.status_code == 200, state.text
        payload = state.json()
        if payload["status"] in {"complete", "failed"}:
            terminal = payload
            break
        time.sleep(0.25)

    assert terminal is not None
    assert terminal["status"] == "complete"
    assert terminal["output_payload"]["explanation"] == "processed by test builtin worker"


def test_registry_lists_new_builtin_agents(client):
    listed = client.get("/registry/agents", headers=_auth_headers(TEST_MASTER_KEY))
    assert listed.status_code == 200, listed.text
    names = {agent["name"] for agent in listed.json()["agents"]}
    assert {
        "arXiv Research Agent",
        "Python Code Executor",
        "Web Researcher Agent",
        "CVE Lookup Agent",
        "Image Generator Agent",
    }.issubset(names)


def test_registry_hides_deprecated_builtin_agents(client):
    listed = client.get("/registry/agents", headers=_auth_headers(TEST_MASTER_KEY))
    assert listed.status_code == 200, listed.text
    names = {agent["name"] for agent in listed.json()["agents"]}
    # Pure LLM wrappers removed from public marketplace
    assert "Resume Analyzer Agent" not in names
    assert "Email Sequence Writer Agent" not in names
    assert "Text Intelligence Agent" not in names
    assert "Negotiation Strategist Agent" not in names
    assert "Scenario Simulator Agent" not in names
    assert "Product Strategy Lab Agent" not in names
    assert "Portfolio Planner Agent" not in names
    assert "Video Storyboard Generator Agent" not in names
    assert "Healthcare Expert Agent" not in names
    assert "System Design Reviewer Agent" not in names
    assert "Incident Response Commander Agent" not in names


def test_builtin_agents_registered_to_system_owner_with_internal_endpoints(client):
    with auth._conn() as conn:
        system_row = conn.execute(
            "SELECT user_id, status FROM users WHERE username = ? LIMIT 1",
            ("system",),
        ).fetchone()
    assert system_row is not None
    assert str(system_row["status"]).lower() == "suspended"
    system_owner = f"user:{system_row['user_id']}"

    for builtin_id in server._CURATED_BUILTIN_AGENT_IDS:
        agent = registry.get_agent(builtin_id, include_unapproved=True)
        assert agent is not None
        assert agent["owner_id"] == system_owner
        assert str(agent["endpoint_url"]).startswith("internal://")
        assert float(agent["price_per_call_usd"]) > 0
        assert isinstance(agent.get("output_examples"), list)
        assert len(agent["output_examples"]) >= 1

    for deprecated_id in (
        server._TEXTINTEL_AGENT_ID,
        server._NEGOTIATION_AGENT_ID,
        server._SCENARIO_AGENT_ID,
        server._PRODUCT_AGENT_ID,
        server._PORTFOLIO_AGENT_ID,
        server._RESUME_AGENT_ID,
        server._EMAILWRITER_AGENT_ID,
        server._SQLBUILDER_AGENT_ID,
        server._DATAINSIGHTS_AGENT_ID,
        server._SECRETS_AGENT_ID,
        server._STATICANALYSIS_AGENT_ID,
        server._DEPSCANNER_AGENT_ID,
        server._SYSTEM_DESIGN_AGENT_ID,
        server._INCIDENT_RESPONSE_AGENT_ID,
        server._HEALTHCARE_EXPERT_AGENT_ID,
        server._VIDEO_STORYBOARD_AGENT_ID,
    ):
        agent = registry.get_agent(deprecated_id, include_unapproved=True)
        if agent is not None:
            assert agent["status"] == "suspended"


def test_registry_call_routes_internal_builtin_without_http_and_records_job(client, monkeypatch):
    caller = _register_user()
    _fund_user_wallet(caller, 100)

    monkeypatch.setattr(
        server.agent_cve_lookup,
        "run",
        lambda payload: {"results": [], "total_vulnerable": 0, "total_packages_checked": 1, "summary": "internal::ok", "source": "nvd"},
    )

    def _fail_post(*args, **kwargs):
        raise AssertionError("registry_call should not use outbound HTTP for internal:// endpoints")

    monkeypatch.setattr(server.http, "post", _fail_post)

    call = client.post(
        f"/registry/agents/{server._CVELOOKUP_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"packages": ["lodash@4.17.21"]},
    )
    assert call.status_code == 200, call.text
    assert call.json()["summary"] == "internal::ok"

    caller_owner = f"user:{caller['user_id']}"
    jobs_for_caller = jobs.list_jobs_for_owner(caller_owner, limit=20)
    synced = [item for item in jobs_for_caller if item["agent_id"] == server._CVELOOKUP_AGENT_ID]
    assert synced
    assert synced[0]["status"] == "complete"
    assert synced[0]["output_payload"]["summary"] == "internal::ok"
    settled = _force_settle_completed_job(synced[0]["job_id"])
    assert settled["settled_at"] is not None

    caller_wallet = payments.get_or_create_wallet(caller_owner)
    agent_wallet = payments.get_or_create_wallet(f"agent:{server._CVELOOKUP_AGENT_ID}")
    # CVE Lookup is priced at $0.06 + $0.01 platform fee = $0.07 total
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] < 100
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] >= 1


def test_registry_call_normalizes_protocol_envelope_for_builtin_responses(client, monkeypatch):
    caller = _register_user()
    _fund_user_wallet(caller, 100)
    captured: dict[str, dict] = {}

    def _run(payload: dict) -> dict:
        captured["payload"] = payload
        return {
            "stdout": "result",
            "exit_code": 0,
            "artifacts": [
                {
                    "name": "result.json",
                    "mime": "application/json",
                    "url_or_base64": "https://example.com/result.json",
                    "size_bytes": 42,
                }
            ],
        }

    monkeypatch.setattr(server.agent_python_executor, "run", _run)
    response = client.post(
        f"/registry/agents/{server._PYTHON_EXECUTOR_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "code": "print('test')",
            "protocol": {
                "input_artifacts": [
                    {
                        "name": "requirements.json",
                        "mime": "application/json",
                        "url_or_base64": "https://example.com/requirements.json",
                        "size_bytes": 10,
                    }
                ],
                "preferred_output_formats": ["application/json"],
                "communication_channel": "analysis",
                "metadata": {"request_id": "req-42"},
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["protocol"]["output_artifacts"][0]["mime"] == "application/json"
    assert body["protocol"]["metadata"]["requested_output_formats"] == ["application/json"]

    sent_protocol = captured["payload"]["protocol"]
    assert sent_protocol["communication_channel"] == "analysis"
    assert sent_protocol["metadata"]["request_id"] == "req-42"
    assert sent_protocol["preferred_output_formats"] == ["application/json"]


def test_image_generator_builtin_accepts_multimodal_input_and_returns_artifact(client, monkeypatch):
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    monkeypatch.setattr(
        server.agent_image_generator,
        "_generate_image_artifact",
        lambda **kwargs: {
            "provider": "openai",
            "model": "gpt-image-1",
            "artifact": {
                "name": "generated.png",
                "mime": "image/png",
                "url_or_base64": "data:image/png;base64,AAAA",
                "size_bytes": 4,
            },
            "warnings": [],
            "generation_prompt": kwargs["prompt"],
        },
    )
    response = client.post(
        f"/registry/agents/{server._IMAGE_GENERATOR_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "prompt": "Minimal logo concept for a robotics startup",
            "style": "flat vector",
            "width": 768,
            "height": 768,
            "input_images": [
                {
                    "mime": "image/png",
                    "url_or_base64": "https://example.com/reference-logo.png",
                    "role": "style_reference",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["input_images_used"] == 1
    assert isinstance(body.get("artifacts"), list) and body["artifacts"]
    artifact = body["artifacts"][0]
    assert artifact["mime"] == "image/png"
    assert str(artifact["url_or_base64"]).startswith("data:image/png;base64,")


def test_mcp_tools_manifest_exposes_registered_agent_schema(client):
    owner = _register_user()
    agent_name = f"MCP Tool Agent {uuid.uuid4().hex[:6]}"
    agent_description = "MCP manifest integration test agent."
    response = client.post(
        "/registry/register",
        headers=_auth_headers(owner["raw_api_key"]),
        json={
            "name": agent_name,
            "description": agent_description,
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.05,
            "tags": ["mcp-test"],
            "input_schema": {
                "fields": [
                    {"name": "task", "type": "string", "required": True},
                    {"name": "depth", "type": "integer"},
                ]
            },
            "output_schema": {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
            },
            "output_examples": [{"input": {"task": "x"}, "output": {"result": "y"}}],
        },
    )
    assert response.status_code == 201, response.text

    manifest_resp = client.get("/mcp/tools", headers=_auth_headers(owner["raw_api_key"]))
    assert manifest_resp.status_code == 200, manifest_resp.text
    body = manifest_resp.json()
    assert body["count"] == len(body["tools"])
    tool = next((item for item in body["tools"] if item["description"] == agent_description), None)
    assert tool is not None
    assert tool["name"] == agent_name.lower().replace(" ", "_")
    assert tool["input_schema"]["fields"][0]["name"] == "task"
    assert tool["output_schema"]["properties"]["result"]["type"] == "string"


def test_mcp_tools_only_returns_active_agents(client):
    owner = _register_user()
    active_name = f"MCP Active {uuid.uuid4().hex[:6]}"
    suspended_name = f"MCP Suspended {uuid.uuid4().hex[:6]}"
    active_agent_id = _register_agent_via_api(client, owner["raw_api_key"], name=active_name)
    suspended_agent_id = _register_agent_via_api(client, owner["raw_api_key"], name=suspended_name)
    registry.set_agent_status(suspended_agent_id, "suspended")
    assert registry.get_agent(active_agent_id)["status"] == "active"
    assert registry.get_agent(suspended_agent_id)["status"] == "suspended"

    response = client.get("/mcp/tools", headers=_auth_headers(owner["raw_api_key"]))
    assert response.status_code == 200, response.text
    names = {tool["name"] for tool in response.json()["tools"]}
    assert active_name.lower().replace(" ", "_") in names
    assert suspended_name.lower().replace(" ", "_") not in names


def test_mcp_tools_defaults_input_schema_when_null(client, monkeypatch):
    owner = _register_user()
    agent_name = f"MCP Null Input {uuid.uuid4().hex[:6]}"
    slug = agent_name.lower().replace(" ", "_")
    monkeypatch.setattr(
        server.registry,
        "get_agents",
        lambda include_internal=True, include_banned=True: [
            {
                "agent_id": str(uuid.uuid4()),
                "name": agent_name,
                "description": "null schema test",
                "status": "active",
                "input_schema": None,
                "output_schema": {"type": "object"},
            }
        ],
    )
    response = client.get("/mcp/tools", headers=_auth_headers(owner["raw_api_key"]))
    assert response.status_code == 200, response.text
    tool = next((item for item in response.json()["tools"] if item["name"] == slug), None)
    assert tool is not None
    assert tool["input_schema"] == {"type": "object", "properties": {}}


def test_mcp_invoke_delegates_to_registry_call_path(client, monkeypatch):
    caller = _register_user()
    _fund_user_wallet(caller, 100)
    monkeypatch.setattr(
        server.agent_python_executor,
        "run",
        lambda payload: {"stdout": "mcp::ok\n", "stderr": "", "exit_code": 0, "timed_out": False,
                         "execution_time_ms": 5, "explanation": "mcp::ok", "variables_captured": {}},
    )

    response = client.post(
        "/mcp/invoke",
        json={
            "tool_name": "python_code_executor",
            "input": {"code": "print('mcp::ok')"},
            "api_key": caller["raw_api_key"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body.get("content"), list) and body["content"]
    assert body["content"][0]["type"] == "text"
    assert "mcp::ok" in body["content"][0]["text"]
    assert body["structuredContent"]["explanation"] == "mcp::ok"

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 99


def test_mcp_invoke_emits_image_content_for_artifact_outputs(client, monkeypatch):
    caller = _register_user()
    _fund_user_wallet(caller, 100)
    monkeypatch.setattr(
        server.agent_image_generator,
        "_generate_image_artifact",
        lambda **kwargs: {
            "provider": "openai",
            "model": "gpt-image-1",
            "artifact": {
                "name": "generated.png",
                "mime": "image/png",
                "url_or_base64": "data:image/png;base64,AAAA",
                "size_bytes": 4,
            },
            "warnings": [],
            "generation_prompt": kwargs["prompt"],
        },
    )
    response = client.post(
        "/mcp/invoke",
        json={
            "tool_name": "image_generator_agent",
            "input": {"prompt": "Simple gradient logo"},
            "api_key": caller["raw_api_key"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    content = body.get("content") or []
    assert any(item.get("type") == "image" for item in content)
    assert body["structuredContent"]["artifacts"][0]["mime"] == "image/png"


def test_python_executor_builtin_runs_code_and_returns_output(client, monkeypatch):
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    monkeypatch.setattr(
        server.agent_python_executor,
        "run",
        lambda payload: {
            "stdout": "1024\n",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "execution_time_ms": 12,
            "explanation": "2**10 equals 1024.",
            "variables_captured": {},
        },
    )
    response = client.post(
        f"/registry/agents/{server._PYTHON_EXECUTOR_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"code": "print(2**10)", "explain": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stdout"] == "1024\n"
    assert body["exit_code"] == 0
    assert "1024" in body["explanation"]


def test_mcp_manifest_returns_server_manifest_shape(client):
    owner = _register_user()
    tools_resp = client.get("/mcp/tools", headers=_auth_headers(owner["raw_api_key"]))
    assert tools_resp.status_code == 200, tools_resp.text
    tool_names = {tool["name"] for tool in tools_resp.json()["tools"]}
    assert "quality_judge_agent" not in tool_names
    manifest_resp = client.get("/mcp/manifest", headers=_auth_headers(owner["raw_api_key"]))
    assert manifest_resp.status_code == 200, manifest_resp.text
    manifest = manifest_resp.json()
    assert manifest["schema_version"] == "v1"
    assert manifest["name"] == "aztea"
    assert "specialized agents as callable tools" in manifest["description"]
    assert manifest["tools"] == tools_resp.json()["tools"]


def test_output_schema_mismatch_returns_schema_mismatch_error(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Schema Agent {uuid.uuid4().hex[:6]}",
        output_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    claim = client.post(
        f"/jobs/{created['job_id']}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim.status_code == 200, claim.text
    response = client.post(
        f"/jobs/{created['job_id']}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"wrong": True}, "claim_token": claim.json()["claim_token"]},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "schema.mismatch"
    assert body["details"]["mismatches"]


def test_agent_scoped_key_claims_and_completes_only_its_agent(client):
    owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    agent_a = _register_agent_via_api(client, owner["raw_api_key"], name=f"Scoped A {uuid.uuid4().hex[:6]}")
    agent_b = _register_agent_via_api(client, owner["raw_api_key"], name=f"Scoped B {uuid.uuid4().hex[:6]}")

    key_resp = client.post(
        f"/registry/agents/{agent_a}/keys",
        headers=_auth_headers(owner["raw_api_key"]),
        json={"name": "scoped-a-key"},
    )
    assert key_resp.status_code == 201, key_resp.text
    created_key = key_resp.json()
    agent_key = created_key["raw_key"]

    listed = client.get(
        f"/registry/agents/{agent_a}/keys",
        headers=_auth_headers(owner["raw_api_key"]),
    )
    assert listed.status_code == 200, listed.text
    keys = listed.json()["keys"]
    assert any(item["key_id"] == created_key["key_id"] and item["is_active"] is True for item in keys)

    denied_list = client.get(
        f"/registry/agents/{agent_a}/keys",
        headers=_auth_headers(agent_key),
    )
    assert denied_list.status_code == 403

    job_a = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_a)
    claim_a = client.post(
        f"/jobs/{job_a['job_id']}/claim",
        headers=_auth_headers(agent_key),
        json={"lease_seconds": 60},
    )
    assert claim_a.status_code == 200, claim_a.text
    complete_a = client.post(
        f"/jobs/{job_a['job_id']}/complete",
        headers=_auth_headers(agent_key),
        json={"output_payload": {"ok": True}, "claim_token": claim_a.json()["claim_token"]},
    )
    assert complete_a.status_code == 200, complete_a.text
    assert complete_a.json()["status"] in {"complete", "failed"}

    job_b = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_b)
    claim_b = client.post(
        f"/jobs/{job_b['job_id']}/claim",
        headers=_auth_headers(agent_key),
        json={"lease_seconds": 60},
    )
    assert claim_b.status_code == 403


def test_orchestrator_agent_can_hire_specialist_agent_programmatically(client):
    orchestrator_owner = _register_user()
    specialist_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 1_000)
    _fund_user_wallet(orchestrator_owner, 200)

    orchestrator_agent_id = _register_agent_via_api(
        client,
        orchestrator_owner["raw_api_key"],
        name=f"Orchestrator {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["orchestrator"],
    )
    specialist_agent_id = _register_agent_via_api(
        client,
        specialist_owner["raw_api_key"],
        name=f"Specialist {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["specialist"],
    )

    parent_job = _create_job_via_api(
        client,
        caller["raw_api_key"],
        agent_id=orchestrator_agent_id,
        max_attempts=2,
    )
    parent_job_id = parent_job["job_id"]
    parent_claim = client.post(
        f"/jobs/{parent_job_id}/claim",
        headers=_auth_headers(orchestrator_owner["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert parent_claim.status_code == 200, parent_claim.text
    parent_claim_token = parent_claim.json()["claim_token"]

    delegated = client.post(
        "/jobs",
        headers=_auth_headers(orchestrator_owner["raw_api_key"]),
        json={
            "agent_id": specialist_agent_id,
            "input_payload": {"task": "solve delegated sub-task"},
            "max_attempts": 2,
        },
    )
    assert delegated.status_code == 201, delegated.text
    child_job_id = delegated.json()["job_id"]

    child_claim = client.post(
        f"/jobs/{child_job_id}/claim",
        headers=_auth_headers(specialist_owner["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert child_claim.status_code == 200, child_claim.text
    child_complete = client.post(
        f"/jobs/{child_job_id}/complete",
        headers=_auth_headers(specialist_owner["raw_api_key"]),
        json={
            "output_payload": {"delegate_result": "specialist complete"},
            "claim_token": child_claim.json()["claim_token"],
        },
    )
    assert child_complete.status_code == 200, child_complete.text
    assert child_complete.json()["status"] == "complete"

    child_state = client.get(
        f"/jobs/{child_job_id}",
        headers=_auth_headers(orchestrator_owner["raw_api_key"]),
    )
    assert child_state.status_code == 200, child_state.text
    assert child_state.json()["status"] == "complete"

    parent_complete = client.post(
        f"/jobs/{parent_job_id}/complete",
        headers=_auth_headers(orchestrator_owner["raw_api_key"]),
        json={
            "output_payload": {
                "delegate_job_id": child_job_id,
                "delegate_result": child_state.json()["output_payload"],
            },
            "claim_token": parent_claim_token,
        },
    )
    assert parent_complete.status_code == 200, parent_complete.text
    assert parent_complete.json()["status"] == "complete"

    caller_view = client.get(
        f"/jobs/{parent_job_id}",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert caller_view.status_code == 200, caller_view.text
    assert caller_view.json()["status"] == "complete"
    assert caller_view.json()["output_payload"]["delegate_job_id"] == child_job_id


