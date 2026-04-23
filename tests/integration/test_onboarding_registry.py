"""Server integration tests (auto-split fragment 2/6)."""

from tests.integration.support import *  # noqa: F403

def test_idempotency_key_replays_rating_response(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Idempotent Rating Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["idempotency-rating"],
    )
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = job["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200
    claim_token = claim.json()["claim_token"]

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert completed.status_code == 200

    idem_headers = {
        **_auth_headers(caller["raw_api_key"]),
        "Idempotency-Key": "rating-idem-1",
    }
    first = client.post(
        f"/jobs/{job_id}/rating",
        headers=idem_headers,
        json={"rating": 5},
    )
    assert first.status_code == 201, first.text

    second = client.post(
        f"/jobs/{job_id}/rating",
        headers=idem_headers,
        json={"rating": 5},
    )
    assert second.status_code == 201
    assert first.json() == second.json()

    with reputation._conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM job_quality_ratings WHERE job_id = ?",
            (job_id,),
        ).fetchone()["count"]
    assert count == 1


def test_job_access_and_worker_auth_are_enforced(client):
    worker_owner = _register_user()
    worker_other = _register_user()
    caller = _register_user()
    outsider = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker_owner["raw_api_key"],
        name=f"Secure Worker Agent {uuid.uuid4().hex[:6]}",
        tags=["security-worker"],
    )
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    job_id = job["job_id"]

    forbidden_claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker_other["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert forbidden_claim.status_code == 409

    owner_claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker_owner["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert owner_claim.status_code == 200

    forbidden_complete = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker_other["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": owner_claim.json()["claim_token"]},
    )
    assert forbidden_complete.status_code == 403

    outsider_get = client.get(f"/jobs/{job_id}", headers=_auth_headers(outsider["raw_api_key"]))
    assert outsider_get.status_code == 403

    caller_get = client.get(f"/jobs/{job_id}", headers=_auth_headers(caller["raw_api_key"]))
    assert caller_get.status_code == 200


def test_jobs_list_supports_cursor_pagination(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Pagination Agent {uuid.uuid4().hex[:6]}",
        tags=["pagination"],
    )

    for _ in range(5):
        created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
        assert created["agent_id"] == agent_id

    page1 = client.get("/jobs?limit=2", headers=_auth_headers(caller["raw_api_key"]))
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert len(body1["jobs"]) == 2
    assert body1["next_cursor"] is not None

    page2 = client.get(
        f"/jobs?limit=2&cursor={body1['next_cursor']}",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert page2.status_code == 200, page2.text
    body2 = page2.json()
    assert len(body2["jobs"]) == 2
    assert body2["next_cursor"] is not None

    ids1 = {item["job_id"] for item in body1["jobs"]}
    ids2 = {item["job_id"] for item in body2["jobs"]}
    assert ids1.isdisjoint(ids2)

    invalid = client.get("/jobs?cursor=not-a-valid-cursor", headers=_auth_headers(caller["raw_api_key"]))
    assert invalid.status_code == 422


def test_quality_rating_and_trust_ranking(client):
    worker_high = _register_user()
    worker_low = _register_user()
    caller = _register_user()
    outsider = _register_user()
    _fund_user_wallet(caller, 400)

    agent_high = _register_agent_via_api(
        client,
        worker_high["raw_api_key"],
        name=f"Trust High {uuid.uuid4().hex[:6]}",
        tags=["trust-int"],
    )
    agent_low = _register_agent_via_api(
        client,
        worker_low["raw_api_key"],
        name=f"Trust Low {uuid.uuid4().hex[:6]}",
        tags=["trust-int"],
    )

    job_high = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_high)
    claim_high = client.post(
        f"/jobs/{job_high['job_id']}/claim",
        headers=_auth_headers(worker_high["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim_high.status_code == 200
    done_high = client.post(
        f"/jobs/{job_high['job_id']}/complete",
        headers=_auth_headers(worker_high["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim_high.json()["claim_token"]},
    )
    assert done_high.status_code == 200

    job_low = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_low)
    claim_low = client.post(
        f"/jobs/{job_low['job_id']}/claim",
        headers=_auth_headers(worker_low["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim_low.status_code == 200
    done_low = client.post(
        f"/jobs/{job_low['job_id']}/complete",
        headers=_auth_headers(worker_low["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim_low.json()["claim_token"]},
    )
    assert done_low.status_code == 200

    rate_high = client.post(
        f"/jobs/{job_high['job_id']}/rating",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"rating": 5},
    )
    assert rate_high.status_code == 201, rate_high.text

    rate_low = client.post(
        f"/jobs/{job_low['job_id']}/rating",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"rating": 1},
    )
    assert rate_low.status_code == 201, rate_low.text

    duplicate = client.post(
        f"/jobs/{job_high['job_id']}/rating",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"rating": 4},
    )
    assert duplicate.status_code == 409

    forbidden = client.post(
        f"/jobs/{job_high['job_id']}/rating",
        headers=_auth_headers(outsider["raw_api_key"]),
        json={"rating": 5},
    )
    assert forbidden.status_code == 403

    ranked = client.get(
        "/registry/agents?tag=trust-int&rank_by=trust",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert ranked.status_code == 200, ranked.text
    agents = ranked.json()["agents"]
    assert len(agents) == 2
    assert all("trust_score" in item for item in agents)
    by_id = {item["agent_id"]: item for item in agents}
    assert by_id[agent_high]["trust_score"] > by_id[agent_low]["trust_score"]
    assert agents[0]["agent_id"] == agent_high


def test_onboarding_validation_ingestion_and_spec_endpoint(client):
    user = _register_user()
    manifest = _manifest(
        name=f"Manifest Agent {uuid.uuid4().hex[:6]}",
        endpoint_url=f"https://manifest.example.com/{uuid.uuid4().hex[:8]}",
    )

    spec = client.get("/agent.md")
    assert spec.status_code == 200
    assert "Registration Metadata" in spec.text

    alias = client.get("/onboarding/spec")
    assert alias.status_code == 200
    assert "Registry Endpoint" in alias.text

    validated = client.post(
        "/onboarding/validate",
        headers=_auth_headers(user["raw_api_key"]),
        json={"manifest_content": manifest},
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["registration_metadata"]["tags"] == ["manifest-test"]

    ingested = client.post(
        "/onboarding/ingest",
        headers=_auth_headers(user["raw_api_key"]),
        json={"manifest_content": manifest},
    )
    assert ingested.status_code == 201, ingested.text
    body = ingested.json()
    assert body["registration_payload"]["name"].startswith("Manifest Agent")
    agent_id = body["agent_id"]
    stored = registry.get_agent(agent_id)
    assert stored is not None
    assert stored["owner_id"] == f"user:{user['user_id']}"


def test_onboarding_manifest_maps_output_schema_and_verifier_url(client):
    user = _register_user()
    output_schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
        "additionalProperties": False,
    }
    verifier_url = f"https://verifier.example.com/{uuid.uuid4().hex[:8]}"
    manifest = _manifest(
        name=f"Manifest Output Agent {uuid.uuid4().hex[:6]}",
        endpoint_url=f"https://manifest.example.com/{uuid.uuid4().hex[:8]}",
        output_schema=output_schema,
        output_verifier_url=verifier_url,
    )

    validated = client.post(
        "/onboarding/validate",
        headers=_auth_headers(user["raw_api_key"]),
        json={"manifest_content": manifest},
    )
    assert validated.status_code == 200, validated.text
    metadata = validated.json()["registration_metadata"]
    assert metadata["output_schema"] == output_schema
    assert metadata["output_verifier_url"] == verifier_url

    ingested = client.post(
        "/onboarding/ingest",
        headers=_auth_headers(user["raw_api_key"]),
        json={"manifest_content": manifest},
    )
    assert ingested.status_code == 201, ingested.text
    stored = registry.get_agent(ingested.json()["agent_id"])
    assert stored is not None
    assert stored["output_schema"] == output_schema
    assert stored["output_verifier_url"] == verifier_url


def test_registry_register_auto_verifies_with_verifier_url(client, monkeypatch):
    worker = _register_user()
    verifier_url = f"https://verifier.aztea.dev/{uuid.uuid4().hex[:8]}"
    captured: dict[str, object] = {}

    class _VerifierResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"verified": True, "reason": "Verifier accepted registration payload."}

    def _fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        captured["url"] = url
        captured["body"] = json
        captured["allow_redirects"] = allow_redirects
        return _VerifierResponse()

    monkeypatch.setattr(server.http, "post", _fake_post)
    response = client.post(
        "/registry/register",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "name": f"Verified Agent {uuid.uuid4().hex[:6]}",
            "description": "Verifier backed agent listing",
            "endpoint_url": f"https://agents.aztea.dev/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.1,
            "tags": ["verified-test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "title": "Task",
                        "description": "verifier input",
                    }
                },
            },
            "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            "output_verifier_url": verifier_url,
            "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["agent"]["verified"] is True
    assert captured["url"] == verifier_url
    assert captured["allow_redirects"] is False
    verifier_payload = captured["body"]
    assert verifier_payload["event_type"] == "agent_registration_verification"
    assert verifier_payload["agent"]["name"].startswith("Verified Agent")


def test_endpoint_health_monitor_marks_degraded_and_recovers(client, monkeypatch):
    worker = _register_user()
    response = client.post(
        "/registry/register",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "name": f"Health Mon Agent {uuid.uuid4().hex[:6]}",
            "description": "Agent for endpoint health monitoring tests",
            "endpoint_url": f"https://health.aztea.dev/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.1,
            "tags": ["health-monitor"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "title": "Task",
                        "description": "health monitor input",
                    }
                },
            },
            "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
        },
    )
    assert response.status_code == 201, response.text
    agent_id = response.json()["agent_id"]

    def _failed_head(*args, **kwargs):
        raise server.http.RequestException("network down")

    monkeypatch.setattr(server.http, "head", _failed_head)
    monkeypatch.setattr(server.http, "get", _failed_head)
    for _ in range(3):
        summary = server._monitor_agent_endpoints(limit=100, timeout_seconds=1, failure_threshold=3)
    assert summary["endpoint_degraded_count"] >= 1
    degraded = registry.get_agent(agent_id)
    assert degraded is not None
    assert degraded["endpoint_health_status"] == "degraded"
    assert degraded["endpoint_consecutive_failures"] >= 3

    class _HealthyHead:
        status_code = 200

    monkeypatch.setattr(server.http, "head", lambda *args, **kwargs: _HealthyHead())
    summary = server._monitor_agent_endpoints(limit=100, timeout_seconds=1, failure_threshold=3)
    assert summary["endpoint_healthy_count"] >= 1
    recovered = registry.get_agent(agent_id)
    assert recovered is not None
    assert recovered["endpoint_health_status"] == "healthy"
    assert recovered["endpoint_consecutive_failures"] == 0


def test_shutdown_draining_flag_is_toggleable(client):
    server._set_server_shutting_down(True)
    try:
        assert server._server_is_shutting_down() is True
        response = client.get("/health", headers=_auth_headers(TEST_MASTER_KEY))
        assert response.status_code in {200, 503}
    finally:
        server._set_server_shutting_down(False)
    assert server._server_is_shutting_down() is False


def test_scoped_keys_enforce_caller_and_worker_permissions(client):
    worker_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    worker_agent_id = _register_agent_via_api(
        client,
        worker_owner["raw_api_key"],
        name=f"Scoped Worker Agent {uuid.uuid4().hex[:6]}",
        tags=["scoped-auth"],
    )

    caller_key_resp = client.post(
        "/auth/keys",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"name": "caller-only", "scopes": ["caller"], "per_job_cap_cents": 500},
    )
    assert caller_key_resp.status_code == 201, caller_key_resp.text
    caller_only_key = caller_key_resp.json()["raw_key"]

    worker_key_resp = client.post(
        "/auth/keys",
        headers=_auth_headers(worker_owner["raw_api_key"]),
        json={"name": "worker-only", "scopes": ["worker"]},
    )
    assert worker_key_resp.status_code == 201, worker_key_resp.text
    worker_only_key = worker_key_resp.json()["raw_key"]

    created = client.post(
        "/jobs",
        headers=_auth_headers(caller_only_key),
        json={"agent_id": worker_agent_id, "input_payload": {"task": "scoped"}, "max_attempts": 2},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]

    caller_cannot_claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(caller_only_key),
        json={"lease_seconds": 120},
    )
    assert caller_cannot_claim.status_code == 403
    assert "worker" in caller_cannot_claim.json()["message"]

    worker_cannot_create = client.post(
        "/jobs",
        headers=_auth_headers(worker_only_key),
        json={"agent_id": worker_agent_id, "input_payload": {"task": "blocked"}},
    )
    assert worker_cannot_create.status_code == 403
    assert "caller" in worker_cannot_create.json()["message"]

    claim_ok = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker_only_key),
        json={"lease_seconds": 120},
    )
    assert claim_ok.status_code == 200, claim_ok.text


def test_caller_scoped_key_requires_per_job_cap_on_creation(client):
    user = _register_user()

    missing_cap = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "caller-without-cap", "scopes": ["caller"]},
    )
    assert missing_cap.status_code == 422, missing_cap.text
    assert missing_cap.json()["error"] == "request.validation_error"

    worker_only = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "worker-only", "scopes": ["worker"]},
    )
    assert worker_only.status_code == 201, worker_only.text

    caller_with_cap = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "caller-with-cap", "scopes": ["caller"], "per_job_cap_cents": 250},
    )
    assert caller_with_cap.status_code == 201, caller_with_cap.text
    assert caller_with_cap.json()["per_job_cap_cents"] == 250


def test_api_key_rotation_revokes_old_key_and_keeps_scopes(client):
    user = _register_user()

    created = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "rotating-key", "scopes": ["worker"]},
    )
    assert created.status_code == 201, created.text
    key_id = created.json()["key_id"]
    old_raw = created.json()["raw_key"]

    rotated = client.post(
        f"/auth/keys/{key_id}/rotate",
        headers=_auth_headers(user["raw_api_key"]),
        json={},
    )
    assert rotated.status_code == 201, rotated.text
    new_raw = rotated.json()["raw_key"]
    assert rotated.json()["scopes"] == ["worker"]

    old_me = client.get("/auth/me", headers=_auth_headers(old_raw))
    assert old_me.status_code == 403

    new_me = client.get("/auth/me", headers=_auth_headers(new_raw))
    assert new_me.status_code == 200
    assert new_me.json()["scopes"] == ["worker"]


def test_auth_me_reports_legal_acceptance_required_for_new_user(client):
    user = _register_user()
    response = client.get("/auth/me", headers=_auth_headers(user["raw_api_key"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["legal_acceptance_required"] is True
    assert body["terms_version_current"] == auth.LEGAL_TERMS_VERSION
    assert body["privacy_version_current"] == auth.LEGAL_PRIVACY_VERSION
    assert body["legal_accepted_at"] is None


def test_auth_legal_accept_records_acceptance(client):
    user = _register_user()

    me_before = client.get("/auth/me", headers=_auth_headers(user["raw_api_key"]))
    assert me_before.status_code == 200, me_before.text
    current_terms = me_before.json()["terms_version_current"]
    current_privacy = me_before.json()["privacy_version_current"]

    accepted = client.post(
        "/auth/legal/accept",
        headers=_auth_headers(user["raw_api_key"]),
        json={"terms_version": current_terms, "privacy_version": current_privacy},
    )
    assert accepted.status_code == 200, accepted.text
    accepted_body = accepted.json()
    assert accepted_body["legal_acceptance_required"] is False
    assert accepted_body["terms_version_accepted"] == current_terms
    assert accepted_body["privacy_version_accepted"] == current_privacy
    assert accepted_body["legal_accepted_at"] is not None

    me_after = client.get("/auth/me", headers=_auth_headers(user["raw_api_key"]))
    assert me_after.status_code == 200, me_after.text
    assert me_after.json()["legal_acceptance_required"] is False


def test_auth_legal_accept_rejects_mismatched_versions(client):
    user = _register_user()
    response = client.post(
        "/auth/legal/accept",
        headers=_auth_headers(user["raw_api_key"]),
        json={"terms_version": "1900-01-01", "privacy_version": "1900-01-01"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "auth.legal_version_mismatch"


def test_api_key_max_spend_cap_enforced_on_job_charges(client):
    worker_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker_owner["raw_api_key"],
        name=f"Spend Capped Agent {uuid.uuid4().hex[:6]}",
        price=0.06,
        tags=["spend-cap"],
    )

    capped_key_resp = client.post(
        "/auth/keys",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "name": "capped-caller",
            "scopes": ["caller"],
            "max_spend_cents": 10,
            "per_job_cap_cents": 500,
        },
    )
    assert capped_key_resp.status_code == 201, capped_key_resp.text
    assert capped_key_resp.json()["max_spend_cents"] == 10
    capped_key = capped_key_resp.json()["raw_key"]

    first = client.post(
        "/jobs",
        headers=_auth_headers(capped_key),
        json={"agent_id": agent_id, "input_payload": {"task": "first"}},
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/jobs",
        headers=_auth_headers(capped_key),
        json={"agent_id": agent_id, "input_payload": {"task": "second"}},
    )
    assert second.status_code == 402, second.text
    blocked = second.json()
    assert blocked["error"] == "payment.spend_limit_exceeded"
    assert blocked["details"]["scope"] == "api_key"
    assert blocked["details"]["limit_cents"] == 10


def test_api_key_per_job_cap_blocks_job_creation(client):
    worker_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker_owner["raw_api_key"],
        name=f"Per Job Capped Agent {uuid.uuid4().hex[:6]}",
        price=0.11,
        tags=["per-job-cap"],
    )

    capped_key_resp = client.post(
        "/auth/keys",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"name": "per-job-capped", "scopes": ["caller"], "per_job_cap_cents": 10},
    )
    assert capped_key_resp.status_code == 201, capped_key_resp.text
    capped_key = capped_key_resp.json()["raw_key"]

    blocked = client.post(
        "/jobs",
        headers=_auth_headers(capped_key),
        json={"agent_id": agent_id, "input_payload": {"task": "too-expensive"}},
    )
    assert blocked.status_code == 402, blocked.text
    body = blocked.json()
    assert body["error"] == "payment.spend_limit_exceeded"
    assert body["details"]["scope"] == "api_key_per_job"
    assert body["details"]["limit_cents"] == 10
    assert body["details"]["attempted_cents"] == 11


def test_jobs_above_50_require_verified_contract(client):
    worker_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 10_000)

    agent_id = _register_agent_via_api(
        client,
        worker_owner["raw_api_key"],
        name=f"High Value Unverified Agent {uuid.uuid4().hex[:6]}",
        price=21.00,
        tags=["high-value"],
    )

    blocked = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "high-value"}},
    )
    assert blocked.status_code == 422, blocked.text
    body = blocked.json()
    assert body["error"] == "job.verified_contract_required"

    registry.set_agent_verified(agent_id, True)
    allowed = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "high-value"}},
    )
    assert allowed.status_code == 201, allowed.text


def test_job_creation_rejects_depth_10_or_more(client):
    worker_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 600)

    agent_id = _register_agent_via_api(
        client,
        worker_owner["raw_api_key"],
        name=f"Depth Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["depth"],
    )

    root = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "root"}},
    )
    assert root.status_code == 201, root.text
    root_id = root.json()["job_id"]

    with jobs._conn() as conn:
        conn.execute("UPDATE jobs SET tree_depth = 9 WHERE job_id = ?", (root_id,))

    blocked = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "child"}, "parent_job_id": root_id},
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"] == "job.orchestration_depth_exceeded"


def test_wallet_daily_spend_limit_blocks_new_job_charges(client):
    worker_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    agent_id = _register_agent_via_api(
        client,
        worker_owner["raw_api_key"],
        name=f"Daily Limit Agent {uuid.uuid4().hex[:6]}",
        price=0.06,
        tags=["daily-limit"],
    )

    set_limit = client.post(
        "/wallets/me/daily-spend-limit",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"daily_spend_limit_cents": 10},
    )
    assert set_limit.status_code == 200, set_limit.text
    assert set_limit.json()["daily_spend_limit_cents"] == 10

    first = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "first"}},
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "second"}},
    )
    assert second.status_code == 402, second.text
    blocked = second.json()
    assert blocked["error"] == "payment.spend_limit_exceeded"
    assert blocked["details"]["scope"] == "wallet_daily"
    assert blocked["details"]["limit_cents"] == 10

    clear_limit = client.post(
        "/wallets/me/daily-spend-limit",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"daily_spend_limit_cents": None},
    )
    assert clear_limit.status_code == 200, clear_limit.text
    assert clear_limit.json()["daily_spend_limit_cents"] is None

    third = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "third"}},
    )
    assert third.status_code == 201, third.text


