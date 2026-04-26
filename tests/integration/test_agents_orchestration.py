"""Server integration tests (auto-split fragment 5/6)."""

from tests.integration.support import *  # noqa: F403

def test_orchestrator_receives_child_completion_callback_and_finishes_parent(client, monkeypatch):
    orchestrator_owner = _register_user()
    specialist_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 1_000)
    _fund_user_wallet(orchestrator_owner, 200)

    orchestrator_agent_id = _register_agent_via_api(
        client,
        orchestrator_owner["raw_api_key"],
        name=f"Callback Orchestrator {uuid.uuid4().hex[:6]}",
        tags=["orchestrator", "callback"],
    )
    specialist_agent_id = _register_agent_via_api(
        client,
        specialist_owner["raw_api_key"],
        name=f"Callback Specialist {uuid.uuid4().hex[:6]}",
        tags=["specialist", "callback"],
    )

    callback_url = "https://hooks.example.com/orchestrator-poke"
    callback_secret = "orchestrator-callback-secret"
    callback_requests: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None, allow_redirects=None):
        callback_requests.append({"url": url, "data": data, "headers": headers or {}})
        resp = requests.Response()
        resp.status_code = 204
        resp._content = b""
        return resp

    monkeypatch.setattr(server.http, "post", fake_post)

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
            "input_payload": {"task": "solve delegated callback sub-task"},
            "callback_url": callback_url,
            "callback_secret": callback_secret,
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
            "output_payload": {"delegate_result": "callback specialist complete"},
            "claim_token": child_claim.json()["claim_token"],
        },
    )
    assert child_complete.status_code == 200, child_complete.text
    assert child_complete.json()["status"] == "complete"

    processed = client.post(
        "/ops/jobs/hooks/process",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"limit": 100},
    )
    assert processed.status_code == 200, processed.text
    assert processed.json()["delivered"] >= 1

    callback_match = next((entry for entry in callback_requests if entry["url"] == callback_url), None)
    assert callback_match is not None
    payload_bytes = callback_match["data"]
    assert isinstance(payload_bytes, (bytes, bytearray))
    payload = json.loads(payload_bytes.decode("utf-8"))
    assert payload["job_id"] == child_job_id
    assert payload["status"] == "complete"
    assert payload["output_payload"] == {"delegate_result": "callback specialist complete"}

    expected_signature = "sha256=" + hmac.new(
        callback_secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    assert callback_match["headers"].get("X-Aztea-Signature") == expected_signature

    parent_complete = client.post(
        f"/jobs/{parent_job_id}/complete",
        headers=_auth_headers(orchestrator_owner["raw_api_key"]),
        json={
            "output_payload": {
                "delegate_job_id": child_job_id,
                "delegate_result": payload["output_payload"],
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


def test_output_verification_accept_blocks_then_allows_settlement(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 400)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Verification Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["verification"],
    )
    created = _create_job_via_api(
        client,
        caller["raw_api_key"],
        agent_id=agent_id,
        extra={"output_verification_window_seconds": 3600},
    )
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim.json()["claim_token"]},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["output_verification_status"] == "pending"
    assert jobs.get_job(job_id)["settled_at"] is None

    accepted = client.post(
        f"/jobs/{job_id}/verification",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"decision": "accept"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["output_verification_status"] == "accepted"
    assert accepted.json()["settled_at"] is not None
    settled = jobs.get_job(job_id)
    assert settled is not None
    assert settled["output_verification_status"] == "accepted"
    assert settled["settled_at"] is not None


def test_output_verification_reject_auto_opens_dispute(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 400)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Reject Verification Agent {uuid.uuid4().hex[:6]}",
        tags=["verification-reject"],
    )
    created = _create_job_via_api(
        client,
        caller["raw_api_key"],
        agent_id=agent_id,
        extra={"output_verification_window_seconds": 3600},
    )
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"result": "bad"}, "claim_token": claim.json()["claim_token"]},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["output_verification_status"] == "pending"

    rejected = client.post(
        f"/jobs/{job_id}/verification",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"decision": "reject", "reason": "Output missed required section."},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["output_verification_status"] == "rejected"
    assert jobs.get_job(job_id)["settled_at"] is None

    dispute = client.get(
        f"/jobs/{job_id}/dispute",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert dispute.status_code == 200, dispute.text
    assert dispute.json()["job_id"] == job_id
    assert dispute.json()["side"] == "caller"
    assert dispute.json()["filing_deposit_cents"] == 5
    assert disputes.has_dispute_for_job(job_id)


def test_clarification_timeout_policy_fail_and_proceed(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 600)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Clarification Timeout Agent {uuid.uuid4().hex[:6]}",
        tags=["clarification-timeout"],
    )

    fail_job = _create_job_via_api(
        client,
        caller["raw_api_key"],
        agent_id=agent_id,
        extra={
            "clarification_timeout_seconds": 30,
            "clarification_timeout_policy": "fail",
        },
    )
    proceed_job = _create_job_via_api(
        client,
        caller["raw_api_key"],
        agent_id=agent_id,
        extra={
            "clarification_timeout_seconds": 30,
            "clarification_timeout_policy": "proceed",
        },
    )

    for item in (fail_job, proceed_job):
        claim = client.post(
            f"/jobs/{item['job_id']}/claim",
            headers=_auth_headers(worker["raw_api_key"]),
            json={"lease_seconds": 120},
        )
        assert claim.status_code == 200, claim.text
        asked = client.post(
            f"/jobs/{item['job_id']}/messages",
            headers=_auth_headers(worker["raw_api_key"]),
            json={"type": "clarification_request", "payload": {"question": "Need format details."}},
        )
        assert asked.status_code == 201, asked.text

    past_deadline = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET clarification_deadline_at = ?, updated_at = ? WHERE job_id IN (?, ?)",
            (past_deadline, past_deadline, fail_job["job_id"], proceed_job["job_id"]),
        )

    sweep = client.post(
        "/ops/jobs/sweep",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"retry_delay_seconds": 0, "sla_seconds": 7200, "limit": 200},
    )
    assert sweep.status_code == 200, sweep.text
    summary = sweep.json()
    assert fail_job["job_id"] in summary["clarification_timeout_failed_job_ids"]
    assert proceed_job["job_id"] in summary["clarification_timeout_proceeded_job_ids"]

    failed_state = jobs.get_job(fail_job["job_id"])
    proceeded_state = jobs.get_job(proceed_job["job_id"])
    assert failed_state is not None
    assert proceeded_state is not None
    assert failed_state["status"] == "failed"
    assert failed_state["settled_at"] is not None
    assert proceeded_state["status"] == "running"


def test_parent_child_linkage_and_fail_cascade_policy(client):
    orchestrator_owner = _register_user()
    specialist_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 1_000)
    _fund_user_wallet(orchestrator_owner, 300)

    orchestrator_agent_id = _register_agent_via_api(
        client,
        orchestrator_owner["raw_api_key"],
        name=f"Cascade Parent {uuid.uuid4().hex[:6]}",
        tags=["orchestrator", "cascade"],
    )
    specialist_agent_id = _register_agent_via_api(
        client,
        specialist_owner["raw_api_key"],
        name=f"Cascade Child {uuid.uuid4().hex[:6]}",
        tags=["specialist", "cascade"],
    )

    parent_job = _create_job_via_api(
        client,
        caller["raw_api_key"],
        agent_id=orchestrator_agent_id,
        max_attempts=2,
    )
    parent_job_id = parent_job["job_id"]

    child_cascade = client.post(
        "/jobs",
        headers=_auth_headers(orchestrator_owner["raw_api_key"]),
        json={
            "agent_id": specialist_agent_id,
            "input_payload": {"task": "cascade me"},
            "parent_job_id": parent_job_id,
            "parent_cascade_policy": "fail_children_on_parent_fail",
        },
    )
    assert child_cascade.status_code == 201, child_cascade.text
    assert child_cascade.json()["parent_job_id"] == parent_job_id
    assert child_cascade.json()["parent_cascade_policy"] == "fail_children_on_parent_fail"
    cascade_child_job_id = child_cascade.json()["job_id"]

    child_detached = client.post(
        "/jobs",
        headers=_auth_headers(orchestrator_owner["raw_api_key"]),
        json={
            "agent_id": specialist_agent_id,
            "input_payload": {"task": "leave me pending"},
            "parent_job_id": parent_job_id,
        },
    )
    assert child_detached.status_code == 201, child_detached.text
    detached_child_job_id = child_detached.json()["job_id"]

    parent_claim = client.post(
        f"/jobs/{parent_job_id}/claim",
        headers=_auth_headers(orchestrator_owner["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert parent_claim.status_code == 200, parent_claim.text

    parent_fail = client.post(
        f"/jobs/{parent_job_id}/fail",
        headers=_auth_headers(orchestrator_owner["raw_api_key"]),
        json={
            "error_message": "orchestrator failed",
            "claim_token": parent_claim.json()["claim_token"],
        },
    )
    assert parent_fail.status_code == 200, parent_fail.text
    assert parent_fail.json()["status"] == "failed"

    cascaded_child_state = jobs.get_job(cascade_child_job_id)
    detached_child_state = jobs.get_job(detached_child_job_id)
    assert cascaded_child_state is not None
    assert detached_child_state is not None
    assert cascaded_child_state["status"] == "failed"
    assert cascaded_child_state["settled_at"] is not None
    assert detached_child_state["status"] == "pending"


def test_parent_cascade_policy_requires_parent_job_id(client):
    owner = _register_user()
    _fund_user_wallet(owner, 300)
    agent_id = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"Policy Guard Agent {uuid.uuid4().hex[:6]}",
    )
    created = client.post(
        "/jobs",
        headers=_auth_headers(owner["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "x"},
            "parent_cascade_policy": "fail_children_on_parent_fail",
        },
    )
    assert created.status_code == 422
    assert "parent_cascade_policy requires parent_job_id" in created.text


def test_agent_scoped_key_cannot_create_delegated_jobs(client):
    owner = _register_user()
    other_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    orchestrator_agent_id = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"Scoped Orchestrator {uuid.uuid4().hex[:6]}",
    )
    specialist_agent_id = _register_agent_via_api(
        client,
        other_owner["raw_api_key"],
        name=f"Scoped Specialist {uuid.uuid4().hex[:6]}",
    )

    key_resp = client.post(
        f"/registry/agents/{orchestrator_agent_id}/keys",
        headers=_auth_headers(owner["raw_api_key"]),
        json={"name": "scoped-orchestrator-key"},
    )
    assert key_resp.status_code == 201, key_resp.text
    scoped_key = key_resp.json()["raw_key"]

    delegated = client.post(
        "/jobs",
        headers=_auth_headers(scoped_key),
        json={
            "agent_id": specialist_agent_id,
            "input_payload": {"task": "delegate"},
        },
    )
    assert delegated.status_code == 403, delegated.text
    delegated_body = delegated.json()
    detail_text = str(delegated_body.get("detail", ""))
    message_text = str(delegated_body.get("message", ""))
    assert "scope" in (detail_text + " " + message_text).lower()


def test_agent_suspend_and_ban_enforcement(client):
    owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)
    agent_id = _register_agent_via_api(client, owner["raw_api_key"], name=f"Moderated {uuid.uuid4().hex[:6]}")

    suspended = client.post(
        f"/admin/agents/{agent_id}/suspend",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["status"] == "suspended"

    blocked = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "blocked"}},
    )
    assert blocked.status_code == 503
    assert blocked.json()["error"] == "agent.suspended"

    active = registry.set_agent_status(agent_id, "active")
    assert active is not None
    pending = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 289

    banned = client.post(
        f"/admin/agents/{agent_id}/ban",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert banned.status_code == 200, banned.text
    assert banned.json()["agent"]["status"] == "banned"
    assert banned.json()["ban_summary"]["affected_jobs"] >= 1

    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 300
    listed = client.get("/registry/agents", headers=_auth_headers(TEST_MASTER_KEY))
    ids = {item["agent_id"] for item in listed.json()["agents"]}
    assert agent_id not in ids
    job_state = client.get(f"/jobs/{pending['job_id']}", headers=_auth_headers(caller["raw_api_key"]))
    assert job_state.status_code == 200
    assert job_state.json()["status"] == "failed"


def test_dispute_window_hours_is_enforced_from_job_record(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)
    agent_id = _register_agent_via_api(client, worker["raw_api_key"], name=f"Window Agent {uuid.uuid4().hex[:6]}")
    created = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "x"}, "dispute_window_hours": 1},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]
    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200
    complete = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim.json()["claim_token"]},
    )
    assert complete.status_code == 200, complete.text
    old_completed = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET completed_at = ?, updated_at = ? WHERE job_id = ?",
            (old_completed, old_completed, job_id),
        )
    dispute = client.post(
        f"/jobs/{job_id}/dispute",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"reason": "too old"},
    )
    assert dispute.status_code == 400
    assert dispute.json()["error"] == "dispute.window_closed"


def test_registry_register_marks_new_worker_agents_pending_review(client):
    worker = _register_user()
    response = client.post(
        "/registry/register",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "name": f"Pending Queue Agent {uuid.uuid4().hex[:6]}",
            "description": "awaiting platform review",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.10,
            "tags": ["pending-review"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string", "description": "task text"}}},
            "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["review_status"] == "pending_review"
    assert body["agent"]["review_status"] == "pending_review"
    assert "pending review" in body["message"].lower()


def test_pending_review_agent_hidden_from_public_listing_and_visible_in_admin_queue(client):
    worker = _register_user()
    caller = _register_user()
    register = client.post(
        "/registry/register",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "name": f"Pending Hidden Agent {uuid.uuid4().hex[:6]}",
            "description": "awaiting platform review",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.10,
            "tags": ["pending-hidden"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string", "description": "task text"}}},
            "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
        },
    )
    assert register.status_code == 201, register.text
    pending_agent_id = register.json()["agent_id"]

    listing = client.get(
        "/registry/agents?tag=pending-hidden",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert listing.status_code == 200, listing.text
    assert all(agent["agent_id"] != pending_agent_id for agent in listing.json()["agents"])

    queue = client.get(
        "/admin/agents/review-queue",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert queue.status_code == 200, queue.text
    assert any(agent["agent_id"] == pending_agent_id for agent in queue.json()["agents"])


def test_pending_review_agent_cannot_accept_job_claim(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    register = client.post(
        "/registry/register",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "name": f"Pending Claim Agent {uuid.uuid4().hex[:6]}",
            "description": "awaiting platform review",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.10,
            "tags": ["pending-claim"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string", "description": "task text"}}},
            "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
        },
    )
    assert register.status_code == 201, register.text
    pending_agent_id = register.json()["agent_id"]

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{pending_agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    charge_tx_id = payments.pre_call_charge(caller_wallet["wallet_id"], 0, pending_agent_id)
    job = jobs.create_job(
        agent_id=pending_agent_id,
        caller_owner_id=f"user:{caller['user_id']}",
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        price_cents=0,
        caller_charge_cents=0,
        platform_fee_pct_at_create=int(payments.PLATFORM_FEE_PCT),
        fee_bearer_policy="caller",
        charge_tx_id=charge_tx_id,
        input_payload={"task": "pending claim should fail"},
        agent_owner_id=f"user:{worker['user_id']}",
    )

    claim = client.post(
        f"/jobs/{job['job_id']}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim.status_code == 403
    assert "pending review" in claim.json()["message"].lower()


def test_admin_review_approve_and_reject_paths(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    register = client.post(
        "/registry/register",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "name": f"Review Flow Agent {uuid.uuid4().hex[:6]}",
            "description": "awaiting platform review",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "healthcheck_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}/health",
            "price_per_call_usd": 0.10,
            "tags": ["review-flow"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string", "description": "task text"}}},
            "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
        },
    )
    assert register.status_code == 201, register.text
    pending_agent_id = register.json()["agent_id"]

    probe_calls: list[tuple[str, int]] = []

    def _fake_probe(url: str, timeout_seconds: int):
        probe_calls.append((url, timeout_seconds))
        return True, None

    monkeypatch.setattr(server, "_probe_agent_endpoint_health", _fake_probe)
    approved = client.post(
        f"/admin/agents/{pending_agent_id}/review",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"decision": "approve", "note": "approved by test"},
    )
    assert approved.status_code == 200, approved.text
    approved_body = approved.json()
    assert approved_body["agent"]["review_status"] == "approved"
    assert approved_body["agent"]["reviewed_by"] == "master"
    assert probe_calls
    assert probe_calls[0][0].endswith("/health")

    listing = client.get(
        "/registry/agents?tag=review-flow",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert listing.status_code == 200, listing.text
    assert any(agent["agent_id"] == pending_agent_id for agent in listing.json()["agents"])

    reject_register = client.post(
        "/registry/register",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "name": f"Review Reject Agent {uuid.uuid4().hex[:6]}",
            "description": "awaiting platform review",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.10,
            "tags": ["review-reject"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string", "description": "task text"}}},
            "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
        },
    )
    assert reject_register.status_code == 201, reject_register.text
    reject_agent_id = reject_register.json()["agent_id"]

    rejected = client.post(
        f"/admin/agents/{reject_agent_id}/review",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"decision": "reject", "note": "insufficient details"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["agent"]["review_status"] == "rejected"

    hidden = client.get(
        "/registry/agents?tag=review-reject",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert hidden.status_code == 200, hidden.text
    assert all(agent["agent_id"] != reject_agent_id for agent in hidden.json()["agents"])


def test_built_in_agents_remain_auto_approved(client):
    _ = client
    builtin = registry.get_agent(server._CODEREVIEW_AGENT_ID, include_unapproved=True)
    assert builtin is not None
    assert builtin["review_status"] == "approved"


def test_protocol_version_header_is_always_set(client):
    response = client.get("/health", headers=_auth_headers(TEST_MASTER_KEY))
    assert response.status_code == 200
    assert response.headers.get("X-Aztea-Version") == "1.0"


def test_request_id_echoed_on_response_and_in_error_payload(client):
    rid = f"e2e-{uuid.uuid4().hex[:16]}"
    response = client.post(
        "/auth/login",
        headers={"X-Request-ID": rid, "Content-Type": "application/json"},
        json={},
    )
    assert response.status_code == 422
    assert response.headers.get("X-Request-ID") == rid
    body = response.json()
    assert body.get("request_id") == rid


def test_health_returns_503_when_memory_probe_fails(client, monkeypatch):
    import psutil

    class _BrokenProcess:
        def memory_info(self):
            raise RuntimeError("memory probe failed")

    monkeypatch.setattr(psutil, "Process", lambda: _BrokenProcess())
    response = client.get("/health", headers=_auth_headers(TEST_MASTER_KEY))
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["memory"]["ok"] is False


def test_dispute_window_respects_global_cap_seconds(client, monkeypatch):
    monkeypatch.setattr(server, "_DISPUTE_FILE_WINDOW_SECONDS", 60)
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)
    agent_id = _register_agent_via_api(client, worker["raw_api_key"], name=f"Global Window {uuid.uuid4().hex[:6]}")

    created = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "x"}, "dispute_window_hours": 24},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]
    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200
    complete = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim.json()["claim_token"]},
    )
    assert complete.status_code == 200, complete.text

    old_completed = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET completed_at = ?, updated_at = ? WHERE job_id = ?",
            (old_completed, old_completed, job_id),
        )

    dispute = client.post(
        f"/jobs/{job_id}/dispute",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"reason": "window should be capped globally"},
    )
    assert dispute.status_code == 400
    assert dispute.json()["error"] == "dispute.window_closed"


