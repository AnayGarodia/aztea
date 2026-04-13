import os

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from core import auth
from core import jobs
from core import payments
from core import registry
from core import reputation
import server

TEST_MASTER_KEY = "test-master-key"


def _close_module_conn(module) -> None:
    conn = getattr(module._local, "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(module._local, "conn")
    except AttributeError:
        pass


def _auth_headers(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


def _register_user() -> dict:
    suffix = uuid.uuid4().hex[:8]
    return auth.register_user(
        username=f"user-{suffix}",
        email=f"user-{suffix}@example.com",
        password="password123",
    )


def _register_agent_via_api(
    client: TestClient,
    raw_api_key: str,
    *,
    name: str,
    price: float = 0.10,
    tags: list[str] | None = None,
) -> str:
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(raw_api_key),
        json={
            "name": name,
            "description": "integration test agent",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": price,
            "tags": tags or ["integration-test"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_id"]


def _fund_user_wallet(user: dict, amount_cents: int = 500) -> dict:
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")
    payments.deposit(wallet["wallet_id"], amount_cents, "integration test funds")
    return wallet


def _create_job_via_api(
    client: TestClient,
    raw_api_key: str,
    *,
    agent_id: str,
    max_attempts: int = 3,
) -> dict:
    resp = client.post(
        "/jobs",
        headers=_auth_headers(raw_api_key),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "analyze"},
            "max_attempts": max_attempts,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _manifest(name: str, endpoint_url: str) -> str:
    return f"""# Example Agent Manifest

## Registry Endpoint
Use POST /registry/register.

## Registration Flow
Validate then register.

## Job Acceptance/Claim Flow Expectations
Workers claim with lease + claim_token.

## Settlement Flow Expectations
Success pays out, failure refunds.

## Auth Expectations
Bearer API key auth is required.

## Registration Metadata
```json
{{
  "name": "{name}",
  "description": "Manifest onboarded agent",
  "endpoint_url": "{endpoint_url}",
  "price_per_call_usd": 0.05,
  "tags": ["manifest-test"],
  "input_schema": {{"type": "object", "properties": {{"task": {{"type": "string"}}}}}}
}}
```
"""


@pytest.fixture
def isolated_db(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-server-integration-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation)

    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))

    yield db_path

    for module in modules:
        _close_module_conn(module)

    for suffix in ("", "-shm", "-wal"):
        path = Path(f"{db_path}{suffix}")
        if path.exists():
            path.unlink()


@pytest.fixture
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)
    with TestClient(server.app) as test_client:
        yield test_client


def test_worker_claim_heartbeat_and_complete_with_owner_auth(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Worker Flow Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["worker-flow"],
    )
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = job["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    heartbeat = client.post(
        f"/jobs/{job_id}/heartbeat",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120, "claim_token": claim_token},
    )
    assert heartbeat.status_code == 200, heartbeat.text

    caller_view = client.get(f"/jobs/{job_id}", headers=_auth_headers(caller["raw_api_key"]))
    assert caller_view.status_code == 200
    assert "claim_token" not in caller_view.json()

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "complete"

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 190
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 9
    assert payments.get_wallet(platform_wallet["wallet_id"])["balance_cents"] == 1


def test_idempotency_key_replays_complete_without_double_settlement(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Idempotent Complete Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["idempotency-complete"],
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
    idem_headers = {
        **_auth_headers(worker["raw_api_key"]),
        "Idempotency-Key": "complete-idem-1",
    }

    first = client.post(
        f"/jobs/{job_id}/complete",
        headers=idem_headers,
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/jobs/{job_id}/complete",
        headers=idem_headers,
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert second.status_code == 200, second.text
    assert first.json() == second.json()

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 190
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 9
    assert payments.get_wallet(platform_wallet["wallet_id"])["balance_cents"] == 1

    stored_agent = registry.get_agent(agent_id)
    assert stored_agent is not None
    assert stored_agent["total_calls"] == 1
    assert stored_agent["success_rate"] == 1.0


def test_idempotency_key_rejects_payload_mismatch(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Idempotent Payload Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["idempotency-mismatch"],
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
    idem_headers = {
        **_auth_headers(worker["raw_api_key"]),
        "Idempotency-Key": "complete-idem-mismatch",
    }

    first = client.post(
        f"/jobs/{job_id}/complete",
        headers=idem_headers,
        json={"output_payload": {"result": "v1"}, "claim_token": claim_token},
    )
    assert first.status_code == 200, first.text

    mismatch = client.post(
        f"/jobs/{job_id}/complete",
        headers=idem_headers,
        json={"output_payload": {"result": "v2"}, "claim_token": claim_token},
    )
    assert mismatch.status_code == 409
    assert "different request payload" in mismatch.json()["detail"]


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
        json={"name": "caller-only", "scopes": ["caller"]},
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
    assert "worker" in caller_cannot_claim.json()["detail"]

    worker_cannot_create = client.post(
        "/jobs",
        headers=_auth_headers(worker_only_key),
        json={"agent_id": worker_agent_id, "input_payload": {"task": "blocked"}},
    )
    assert worker_cannot_create.status_code == 403
    assert "caller" in worker_cannot_create.json()["detail"]

    claim_ok = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker_only_key),
        json={"lease_seconds": 120},
    )
    assert claim_ok.status_code == 200, claim_ok.text


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


def test_admin_scope_controls_ops_endpoints(client):
    user = _register_user()

    caller_key_resp = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "caller-only", "scopes": ["caller"]},
    )
    assert caller_key_resp.status_code == 201
    caller_key = caller_key_resp.json()["raw_key"]

    admin_key_resp = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "admin-only", "scopes": ["admin"]},
    )
    assert admin_key_resp.status_code == 201
    admin_key = admin_key_resp.json()["raw_key"]

    blocked_metrics = client.get("/ops/jobs/metrics", headers=_auth_headers(caller_key))
    assert blocked_metrics.status_code == 403

    allowed_metrics = client.get("/ops/jobs/metrics", headers=_auth_headers(admin_key))
    assert allowed_metrics.status_code == 200

    blocked_slo = client.get("/ops/jobs/slo", headers=_auth_headers(caller_key))
    assert blocked_slo.status_code == 403

    allowed_slo = client.get("/ops/jobs/slo", headers=_auth_headers(admin_key))
    assert allowed_slo.status_code == 200
    assert "slo" in allowed_slo.json()

    blocked_sweep = client.post(
        "/ops/jobs/sweep",
        headers=_auth_headers(caller_key),
        json={"retry_delay_seconds": 0, "sla_seconds": 60, "limit": 10},
    )
    assert blocked_sweep.status_code == 403

    allowed_sweep = client.post(
        "/ops/jobs/sweep",
        headers=_auth_headers(admin_key),
        json={"retry_delay_seconds": 0, "sla_seconds": 60, "limit": 10},
    )
    assert allowed_sweep.status_code == 200


def test_payments_reconciliation_and_settlement_trace_endpoints(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Settlement Trace Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["settlement-trace"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert completed.status_code == 200, completed.text

    trace = client.get(
        f"/ops/jobs/{job_id}/settlement-trace",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert trace.status_code == 200, trace.text
    trace_body = trace.json()
    tx_types = {tx["type"] for tx in trace_body["transactions"]}
    assert {"charge", "payout", "fee"}.issubset(tx_types)
    assert trace_body["expected_agent_payout_cents"] == 9
    assert trace_body["expected_platform_fee_cents"] == 1

    preview = client.get(
        "/ops/payments/reconcile",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["invariant_ok"] is True

    run = client.post(
        "/ops/payments/reconcile",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"max_mismatches": 50},
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]

    runs = client.get(
        "/ops/payments/reconcile/runs?limit=5",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert runs.status_code == 200, runs.text
    assert any(item["run_id"] == run_id for item in runs.json()["runs"])


def test_outbound_url_validation_blocks_private_targets_by_default(client):
    user = _register_user()

    hook_resp = client.post(
        "/ops/jobs/hooks",
        headers=_auth_headers(user["raw_api_key"]),
        json={"target_url": "http://127.0.0.1:9999/hook"},
    )
    assert hook_resp.status_code == 422
    assert "private/loopback" in hook_resp.json()["detail"]

    manifest_resp = client.post(
        "/onboarding/validate",
        headers=_auth_headers(user["raw_api_key"]),
        json={"manifest_url": "http://localhost:8000/agent.md"},
    )
    assert manifest_resp.status_code == 422
    assert "localhost" in manifest_resp.json()["detail"]


def test_job_sweeper_handles_timeouts_sla_and_event_hooks(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Ops Sweeper Agent {uuid.uuid4().hex[:6]}",
        tags=["ops-sweeper"],
    )

    hook_events: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None):
        payload = {}
        if data:
            payload = json.loads(data.decode("utf-8"))
        hook_events.append({"url": url, "headers": headers or {}, "payload": payload})
        resp = requests.Response()
        resp.status_code = 204
        resp._content = b""
        return resp

    monkeypatch.setattr(server.http, "post", fake_post)

    hook_resp = client.post(
        "/ops/jobs/hooks",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"target_url": "https://hooks.example.com/jobs"},
    )
    assert hook_resp.status_code == 201, hook_resp.text

    timeout_job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=1)
    timeout_job_id = timeout_job["job_id"]
    claim = client.post(
        f"/jobs/{timeout_job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim.status_code == 200

    sla_job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=1)
    sla_job_id = sla_job["job_id"]

    with jobs._conn() as conn:
        expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_expires_at = ? WHERE job_id = ?",
            (expired, timeout_job_id),
        )
        conn.execute(
            "UPDATE jobs SET created_at = ?, updated_at = ? WHERE job_id = ?",
            (old, old, sla_job_id),
        )

    sweep = client.post(
        "/ops/jobs/sweep",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"retry_delay_seconds": 0, "sla_seconds": 60, "limit": 100},
    )
    assert sweep.status_code == 200, sweep.text
    summary = sweep.json()
    assert timeout_job_id in summary["timeout_failed_job_ids"]
    assert sla_job_id in summary["sla_failed_job_ids"]

    process = client.post(
        "/ops/jobs/hooks/process",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"limit": 200},
    )
    assert process.status_code == 200, process.text

    timeout_state = client.get(f"/jobs/{timeout_job_id}", headers=_auth_headers(caller["raw_api_key"]))
    sla_state = client.get(f"/jobs/{sla_job_id}", headers=_auth_headers(caller["raw_api_key"]))
    assert timeout_state.status_code == 200
    assert sla_state.status_code == 200
    assert timeout_state.json()["status"] == "failed"
    assert sla_state.json()["status"] == "failed"

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 300

    events = client.get("/ops/jobs/events", headers=_auth_headers(caller["raw_api_key"]))
    assert events.status_code == 200
    event_types = {event["event_type"] for event in events.json()["events"]}
    assert "job.timeout_terminal" in event_types
    assert "job.sla_expired" in event_types

    hook_event_types = {entry["payload"].get("event_type") for entry in hook_events}
    assert "job.timeout_terminal" in hook_event_types
    assert "job.sla_expired" in hook_event_types

    metrics = client.get("/ops/jobs/metrics", headers=_auth_headers(TEST_MASTER_KEY))
    assert metrics.status_code == 200
    body = metrics.json()
    assert "status_counts" in body
    assert "alerts" in body
    assert "hook_delivery" in body
    assert "slo" in body


def test_hook_delivery_dead_letter_listing(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    monkeypatch.setattr(server, "_HOOK_DELIVERY_MAX_ATTEMPTS", 1)

    def always_fail_post(url, data=None, headers=None, timeout=None):
        raise requests.RequestException("hook unavailable")

    monkeypatch.setattr(server.http, "post", always_fail_post)

    hook_resp = client.post(
        "/ops/jobs/hooks",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"target_url": "https://hooks.example.com/unavailable"},
    )
    assert hook_resp.status_code == 201, hook_resp.text

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Deadletter Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["dead-letter"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    assert created["agent_id"] == agent_id

    processed = client.post(
        "/ops/jobs/hooks/process",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"limit": 50},
    )
    assert processed.status_code == 200, processed.text

    dead_letters = client.get(
        "/ops/jobs/hooks/dead-letter",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert dead_letters.status_code == 200, dead_letters.text
    assert dead_letters.json()["count"] >= 1
