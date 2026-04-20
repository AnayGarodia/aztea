import os

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

import hashlib
import hmac
import json
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import requests
import uvicorn
from fastapi.testclient import TestClient

from core import auth
from core import disputes
from core import jobs
from core import payments
from core import registry
from core import reputation
from core import error_codes
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


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    output_verifier_url: str | None = None,
    auto_approve: bool = True,
) -> str:
    payload = {
        "name": name,
        "description": "integration test agent",
        "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
        "price_per_call_usd": price,
        "tags": tags or ["integration-test"],
        "input_schema": input_schema or {"type": "object", "properties": {"task": {"type": "string"}}},
    }
    if output_schema is not None:
        payload["output_schema"] = output_schema
    if output_verifier_url is not None:
        payload["output_verifier_url"] = output_verifier_url
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(raw_api_key),
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["agent_id"]
    if auto_approve:
        review = client.post(
            f"/admin/agents/{agent_id}/review",
            headers=_auth_headers(TEST_MASTER_KEY),
            json={"decision": "approve", "note": "test auto-approve"},
        )
        assert review.status_code == 200, review.text
    return agent_id


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
    extra: dict | None = None,
) -> dict:
    payload = {
        "agent_id": agent_id,
        "input_payload": {"task": "analyze"},
        "max_attempts": max_attempts,
    }
    if extra:
        payload.update(extra)
    resp = client.post(
        "/jobs",
        headers=_auth_headers(raw_api_key),
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _force_settle_completed_job(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    assert job is not None
    assert job["status"] == "complete"
    window_seconds = max(1, int(server._effective_dispute_window_seconds(job)))
    completed_at = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds + 5)).isoformat()
    expired_verification_deadline = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET completed_at = ?,
                updated_at = ?,
                output_verification_deadline_at = CASE
                    WHEN output_verification_status = 'pending' THEN ?
                    ELSE output_verification_deadline_at
                END
            WHERE job_id = ?
            """,
            (completed_at, completed_at, expired_verification_deadline, job_id),
        )
    server._sweep_jobs(
        retry_delay_seconds=0,
        sla_seconds=7200,
        limit=100,
        actor_owner_id="test:settlement",
    )
    settled = jobs.get_job(job_id)
    assert settled is not None
    return settled


def _manifest(
    name: str,
    endpoint_url: str,
    *,
    output_schema: dict | None = None,
    output_verifier_url: str | None = None,
) -> str:
    metadata = {
        "name": name,
        "description": "Manifest onboarded agent",
        "endpoint_url": endpoint_url,
        "price_per_call_usd": 0.05,
        "tags": ["manifest-test"],
        "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
    }
    if output_schema is not None:
        metadata["output_schema"] = output_schema
    if output_verifier_url is not None:
        metadata["output_verifier_url"] = output_verifier_url
    metadata_json = json.dumps(metadata, indent=2)

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
{metadata_json}
```
"""


@pytest.fixture
def isolated_db(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-server-integration-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)

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
    settled = _force_settle_completed_job(job_id)
    assert settled["settled_at"] is not None

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 189
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 10
    assert payments.get_wallet(platform_wallet["wallet_id"])["balance_cents"] == 1


def test_worker_complete_after_expired_lease_returns_410_with_timeout_state(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Expired Lease Agent {uuid.uuid4().hex[:6]}",
        tags=["expired-lease"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=1)
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
            (expired, expired, job_id),
        )

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert completed.status_code == 410, completed.text
    body = completed.json()
    assert body["error"] == "job.lease_expired"
    assert body["message"] == "Job lease expired before completion."
    job_data = body["details"]["job"]
    assert job_data["status"] == "failed"
    assert job_data["timeout_count"] == 1
    assert job_data["error_message"] == "Job lease expired before completion."
    assert job_data["claim_owner_id"] is None


def test_complete_called_twice_returns_same_state_without_idempotency_key(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Double Complete Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["double-complete"],
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
    output_payload = {"ok": True, "result": "stable"}

    first = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": output_payload, "claim_token": claim_token},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": output_payload, "claim_token": claim_token},
    )
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    assert second.json()["status"] == "complete"
    assert second.json()["output_payload"] == output_payload
    settled = _force_settle_completed_job(job_id)
    assert settled["settled_at"] is not None

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 189
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 10
    assert payments.get_wallet(platform_wallet["wallet_id"])["balance_cents"] == 1


def test_caller_clarification_after_delay_extends_lease_and_avoids_sweeper_timeout(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Clarification Lease Agent {uuid.uuid4().hex[:6]}",
        tags=["clarification-lease"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 300},
    )
    assert claim.status_code == 200, claim.text

    asked = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "clarification_needed", "payload": {"question": "Need more context."}},
    )
    assert asked.status_code == 201, asked.text

    near_expiry = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
            (near_expiry, near_expiry, job_id),
        )

    answered = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"type": "clarification", "payload": {"answer": "Proceed with latest assumptions."}},
    )
    assert answered.status_code == 201, answered.text

    resumed = jobs.get_job(job_id)
    assert resumed is not None
    assert resumed["status"] == "running"
    assert datetime.fromisoformat(resumed["lease_expires_at"]) > datetime.fromisoformat(near_expiry)

    sweep = client.post(
        "/ops/jobs/sweep",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"retry_delay_seconds": 0, "sla_seconds": 7200, "limit": 100},
    )
    assert sweep.status_code == 200, sweep.text
    summary = sweep.json()
    assert job_id not in summary["timeout_retry_job_ids"]
    assert job_id not in summary["timeout_failed_job_ids"]

    latest = jobs.get_job(job_id)
    assert latest is not None
    assert latest["status"] == "running"
    assert latest["claim_owner_id"] == resumed["claim_owner_id"]


def test_job_message_stream_receives_clarification_request_from_second_client(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)

    port = _free_tcp_port()
    uvicorn_config = uvicorn.Config(
        server.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)
    uvicorn_server.install_signal_handlers = lambda: None
    uvicorn_thread = threading.Thread(target=uvicorn_server.run, name="test-uvicorn-stream", daemon=True)
    uvicorn_thread.start()

    try:
        deadline = time.time() + 5
        while not uvicorn_server.started and uvicorn_thread.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        assert uvicorn_server.started, "uvicorn server did not start in time"

        base_url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base_url, timeout=5.0) as post_client:
            worker = _register_user()
            caller = _register_user()
            _fund_user_wallet(caller, 300)

            agent_id = _register_agent_via_api(
                post_client,
                worker["raw_api_key"],
                name=f"Stream Agent {uuid.uuid4().hex[:6]}",
                tags=["stream-messages"],
            )
            created = _create_job_via_api(
                post_client,
                caller["raw_api_key"],
                agent_id=agent_id,
                max_attempts=2,
            )
            job_id = created["job_id"]

            ready = threading.Event()
            delivered = threading.Event()
            received: dict[str, dict] = {}
            stream_errors: list[str] = []

            def _consume_stream() -> None:
                try:
                    with httpx.Client(base_url=base_url, timeout=None) as stream_client:
                        with stream_client.stream(
                            "GET",
                            f"/jobs/{job_id}/stream",
                            headers=_auth_headers(caller["raw_api_key"]),
                        ) as response:
                            assert response.status_code == 200
                            ready.set()
                            for line in response.iter_lines():
                                if not line or not line.startswith("data: "):
                                    continue
                                received["message"] = json.loads(line[6:])
                                delivered.set()
                                return
                except Exception as exc:  # pragma: no cover - defensive thread capture
                    stream_errors.append(str(exc))
                finally:
                    ready.set()
                    delivered.set()

            stream_thread = threading.Thread(target=_consume_stream, name="job-stream-subscriber")
            stream_thread.start()

            assert ready.wait(timeout=1), "stream subscriber did not connect in time"
            posted = post_client.post(
                f"/jobs/{job_id}/messages",
                headers=_auth_headers(worker["raw_api_key"]),
                json={"type": "clarification_request", "payload": {"question": "Need more context."}},
            )
            assert posted.status_code == 201, posted.text

            assert delivered.wait(timeout=1), "stream subscriber did not receive a message in time"
            stream_thread.join(timeout=1)
            assert not stream_errors
            assert not stream_thread.is_alive()

            event_payload = received.get("message")
            assert event_payload is not None
            assert event_payload["type"] == "clarification_request"
            assert event_payload["payload"]["question"] == "Need more context."
    finally:
        uvicorn_server.should_exit = True
        uvicorn_thread.join(timeout=5)


def test_job_message_protocol_validation_and_correlation_rules(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Typed Message Agent {uuid.uuid4().hex[:6]}",
        tags=["typed-messages"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = created["job_id"]

    claimed = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 300},
    )
    assert claimed.status_code == 200, claimed.text

    near_expiry = (datetime.now(timezone.utc) + timedelta(seconds=45)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
            (near_expiry, near_expiry, job_id),
        )

    progress = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "progress", "payload": {"message": "halfway done", "percent": 50}},
    )
    assert progress.status_code == 201, progress.text
    assert progress.json()["type"] == "progress"

    updated = jobs.get_job(job_id)
    assert updated is not None
    assert datetime.fromisoformat(updated["lease_expires_at"]) >= datetime.fromisoformat(near_expiry)

    invalid_tool_call = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "tool_call", "payload": {"arguments": {"ticker": "AAPL"}}},
    )
    assert invalid_tool_call.status_code == 400
    assert "tool_call" in invalid_tool_call.json()["message"]

    unknown_correlation = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "tool_result", "payload": {"correlation_id": "missing-correlation", "result": {}}},
    )
    assert unknown_correlation.status_code == 400
    assert "Unknown tool_result correlation_id" in unknown_correlation.json()["message"]

    tool_call = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "tool_call", "payload": {"tool_name": "lookup_filing", "arguments": {"ticker": "AAPL"}}},
    )
    assert tool_call.status_code == 201, tool_call.text
    generated_correlation_id = tool_call.json()["payload"]["correlation_id"]
    assert generated_correlation_id

    tool_result = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "type": "tool_result",
            "payload": {"correlation_id": generated_correlation_id, "result": {"ticker": "AAPL"}},
        },
    )
    assert tool_result.status_code == 201, tool_result.text
    assert tool_result.json()["payload"]["correlation_id"] == generated_correlation_id

    unsupported_legacy = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "legacy-custom-message", "payload": {"text": "still works"}},
    )
    assert unsupported_legacy.status_code == 400
    assert "Unsupported job message type" in unsupported_legacy.json()["message"]


def test_concurrent_complete_and_sweeper_timeout_race_has_no_lost_work(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Race Complete Agent {uuid.uuid4().hex[:6]}",
        tags=["race-complete"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
            (expired, expired, job_id),
        )

    start = threading.Event()
    thread_errors: list[str] = []
    results: dict[str, object] = {}

    def _complete() -> None:
        try:
            start.wait()
            resp = client.post(
                f"/jobs/{job_id}/complete",
                headers=_auth_headers(worker["raw_api_key"]),
                json={"output_payload": {"ok": True, "race": "done"}, "claim_token": claim_token},
            )
            results["complete_status"] = resp.status_code
            results["complete_body"] = resp.json()
        except Exception as exc:  # pragma: no cover - defensive thread capture
            thread_errors.append(str(exc))

    def _sweep() -> None:
        try:
            start.wait()
            results["sweep_summary"] = server._sweep_jobs(
                retry_delay_seconds=0,
                sla_seconds=7200,
                limit=100,
                actor_owner_id="test:race",
            )
        except Exception as exc:  # pragma: no cover - defensive thread capture
            thread_errors.append(str(exc))

    complete_thread = threading.Thread(target=_complete, name="race-complete-thread")
    sweep_thread = threading.Thread(target=_sweep, name="race-sweep-thread")
    complete_thread.start()
    sweep_thread.start()
    start.set()
    complete_thread.join(timeout=5)
    sweep_thread.join(timeout=5)

    assert not complete_thread.is_alive()
    assert not sweep_thread.is_alive()
    assert not thread_errors

    first_status = int(results["complete_status"])
    assert first_status in {200, 410}

    final_response = results["complete_body"]
    if first_status == 410:
        retry = client.post(
            f"/jobs/{job_id}/complete",
            headers=_auth_headers(worker["raw_api_key"]),
            json={"output_payload": {"ok": True, "race": "done"}, "claim_token": claim_token},
        )
        assert retry.status_code == 200, retry.text
        final_response = retry.json()

    assert final_response["status"] in {"complete", "failed"}
    if final_response["status"] == "complete":
        assert final_response["output_payload"] == {"ok": True, "race": "done"}
        settled = _force_settle_completed_job(job_id)
        assert settled["settled_at"] is not None
    else:
        assert "lease expired" in (final_response.get("error_message") or "").lower()

    sweep_summary = results["sweep_summary"]
    if final_response["status"] == "complete":
        assert job_id not in sweep_summary["timeout_failed_job_ids"]
    else:
        assert job_id in sweep_summary["timeout_failed_job_ids"]

    final_job = jobs.get_job(job_id)
    assert final_job is not None
    assert final_job["status"] == final_response["status"]
    if final_job["status"] == "failed":
        assert final_job["settled_at"] is not None


def test_master_complete_records_auditable_claim_event(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Master Claim Event Agent {uuid.uuid4().hex[:6]}",
        tags=["master-claim-event"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "complete"

    messages = client.get(f"/jobs/{job_id}/messages", headers=_auth_headers(TEST_MASTER_KEY))
    assert messages.status_code == 200, messages.text
    claim_events = [
        item for item in messages.json()["messages"] if item["type"] == "claim_event"
    ]
    bypass_events = [
        item for item in claim_events if item["payload"].get("event_type") == "master_claim_bypass"
    ]
    assert len(bypass_events) == 1
    event = bypass_events[0]
    assert event["from_id"] == "master"
    assert event["payload"]["claim_owner_id"] == f"user:{worker['user_id']}"
    assert event["payload"]["claim_token_sha256"] == hashlib.sha256(
        claim_token.encode("utf-8")
    ).hexdigest()
    metadata = event["payload"].get("metadata") or {}
    assert metadata.get("action") == "complete"
    assert metadata.get("status") == "running"


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
    settled = _force_settle_completed_job(job_id)
    assert settled["settled_at"] is not None

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 189
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 10
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
    assert "different request payload" in mismatch.json()["message"]


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
            "description": "Verifier-backed listing",
            "endpoint_url": f"https://agents.aztea.dev/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.1,
            "tags": ["verified-test"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
            "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            "output_verifier_url": verifier_url,
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
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
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
        price=51.00,
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


def test_jobs_batch_status_endpoint_returns_aggregate_counts(client):
    worker_owner = _register_user()
    caller = _register_user()
    outsider = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker_owner["raw_api_key"],
        name=f"Batch Status Agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["batch-status"],
    )

    created = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "jobs": [
                {"agent_id": agent_id, "input_payload": {"task": "a"}},
                {"agent_id": agent_id, "input_payload": {"task": "b"}},
            ]
        },
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    assert created_body["count"] == 2
    assert created_body["total_price_cents"] == 12
    batch_id = created_body["batch_id"]

    status = client.get(
        f"/jobs/batch/{batch_id}",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert status.status_code == 200, status.text
    status_body = status.json()
    assert status_body["batch_id"] == batch_id
    assert status_body["count"] == 2
    assert status_body["n_pending"] == 2
    assert status_body["n_complete"] == 0
    assert status_body["n_failed"] == 0
    assert status_body["total_cost_cents"] == 10
    assert all(job["batch_id"] == batch_id for job in status_body["jobs"])

    blocked = client.get(
        f"/jobs/batch/{batch_id}",
        headers=_auth_headers(outsider["raw_api_key"]),
    )
    assert blocked.status_code == 404


def test_admin_scope_controls_ops_endpoints(client):
    user = _register_user()

    caller_key_resp = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "caller-only", "scopes": ["caller"], "per_job_cap_cents": 500},
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
    settled = _force_settle_completed_job(job_id)
    assert settled["settled_at"] is not None

    trace = client.get(
        f"/ops/jobs/{job_id}/settlement-trace",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert trace.status_code == 200, trace.text
    trace_body = trace.json()
    tx_types = {tx["type"] for tx in trace_body["transactions"]}
    assert {"charge", "payout", "fee"}.issubset(tx_types)
    assert trace_body["expected_agent_payout_cents"] == 10
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


def test_fee_distribution_policies_cover_caller_worker_split():
    caller_policy = payments.compute_success_distribution(
        10,
        platform_fee_pct=10,
        fee_bearer_policy="caller",
    )
    assert caller_policy == {
        "caller_charge_cents": 11,
        "agent_payout_cents": 10,
        "platform_fee_cents": 1,
    }

    worker_policy = payments.compute_success_distribution(
        10,
        platform_fee_pct=10,
        fee_bearer_policy="worker",
    )
    assert worker_policy == {
        "caller_charge_cents": 10,
        "agent_payout_cents": 9,
        "platform_fee_cents": 1,
    }

    split_policy = payments.compute_success_distribution(
        25,
        platform_fee_pct=10,
        fee_bearer_policy="split",
    )
    assert split_policy == {
        "caller_charge_cents": 27,
        "agent_payout_cents": 24,
        "platform_fee_cents": 3,
    }


def test_listing_and_job_create_show_caller_all_in_charge(client):
    worker = _register_user()
    caller = _register_user()
    caller_wallet = _fund_user_wallet(caller, 200)
    before_balance = payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"]

    tag = f"all-in-{uuid.uuid4().hex[:6]}"
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"All In Charge Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=[tag],
    )

    listings = client.get(
        f"/registry/agents?tag={tag}",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert listings.status_code == 200, listings.text
    listing_agent = next(agent for agent in listings.json()["agents"] if agent["agent_id"] == agent_id)
    assert listing_agent["caller_charge_cents"] == 11

    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    assert created["caller_charge_cents"] == 11
    assert created["fee_bearer_policy"] == "caller"

    after_balance = payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"]
    assert before_balance - after_balance == 11


def test_topup_session_enforces_daily_limit(client, monkeypatch):
    user = _register_user()
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")

    import sqlite3

    with sqlite3.connect(jobs.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO stripe_sessions (session_id, wallet_id, amount_cents, processed_at) VALUES (?, ?, ?, ?)",
            (
                f"cs_{uuid.uuid4().hex[:10]}",
                wallet["wallet_id"],
                9_500,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    fake_checkout = SimpleNamespace(
        Session=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(url="https://checkout.example/session", id="cs_test_123")
        )
    )
    monkeypatch.setattr(server, "_STRIPE_AVAILABLE", True)
    monkeypatch.setattr(server, "_STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(server, "_TOPUP_DAILY_LIMIT_CENTS", 10_000)
    monkeypatch.setattr(server, "_stripe_lib", SimpleNamespace(api_key=None, checkout=fake_checkout))

    blocked = client.post(
        "/wallets/topup/session",
        headers=_auth_headers(user["raw_api_key"]),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 600},
    )
    assert blocked.status_code == 400, blocked.text
    blocked_body = blocked.json()
    assert blocked_body["error"] == "payment.topup_daily_limit_exceeded"
    assert blocked_body["details"]["limit_cents"] == 10_000

    allowed = client.post(
        "/wallets/topup/session",
        headers=_auth_headers(user["raw_api_key"]),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 500},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["session_id"] == "cs_test_123"


def test_wallet_deposit_enforces_minimum_amount(client):
    user = _register_user()
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")

    below = client.post(
        "/wallets/deposit",
        headers=_auth_headers(user["raw_api_key"]),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 499, "memo": "too low"},
    )
    assert below.status_code == 422, below.text
    below_body = below.json()
    assert below_body["error"] == error_codes.DEPOSIT_BELOW_MINIMUM
    assert below_body["details"]["minimum_cents"] == server.MINIMUM_DEPOSIT_CENTS
    assert below_body["details"]["attempted_cents"] == 499

    allowed = client.post(
        "/wallets/deposit",
        headers=_auth_headers(user["raw_api_key"]),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 500, "memo": "ok"},
    )
    assert allowed.status_code == 200, allowed.text


def test_wallet_topup_session_enforces_minimum_amount(client, monkeypatch):
    user = _register_user()
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")
    fake_checkout = SimpleNamespace(
        Session=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(url="https://checkout.example/session", id="cs_test_minimum")
        )
    )
    monkeypatch.setattr(server, "_STRIPE_AVAILABLE", True)
    monkeypatch.setattr(server, "_STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(server, "_stripe_lib", SimpleNamespace(api_key=None, checkout=fake_checkout))

    below = client.post(
        "/wallets/topup/session",
        headers=_auth_headers(user["raw_api_key"]),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 499},
    )
    assert below.status_code == 422, below.text
    below_body = below.json()
    assert below_body["error"] == error_codes.DEPOSIT_BELOW_MINIMUM
    assert below_body["details"]["minimum_cents"] == server.MINIMUM_DEPOSIT_CENTS
    assert below_body["details"]["attempted_cents"] == 499

    allowed = client.post(
        "/wallets/topup/session",
        headers=_auth_headers(user["raw_api_key"]),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 500},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["session_id"] == "cs_test_minimum"


def test_wallet_withdrawals_returns_only_caller_wallet_history(client):
    user = _register_user()
    other = _register_user()
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")
    other_wallet = payments.get_or_create_wallet(f"user:{other['user_id']}")

    import sqlite3

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(jobs.DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_connect_transfers (
                transfer_id   TEXT PRIMARY KEY,
                wallet_id     TEXT NOT NULL,
                amount_cents  INTEGER NOT NULL,
                stripe_tx_id  TEXT NOT NULL,
                memo          TEXT,
                created_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO stripe_connect_transfers
                (transfer_id, wallet_id, amount_cents, stripe_tx_id, memo, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), wallet["wallet_id"], 1234, "tr_user_123", "Withdrawal to bank", now),
        )
        conn.execute(
            """
            INSERT INTO stripe_connect_transfers
                (transfer_id, wallet_id, amount_cents, stripe_tx_id, memo, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), other_wallet["wallet_id"], 4321, "tr_other_456", "Other withdrawal", now),
        )
        conn.commit()

    response = client.get("/wallets/withdrawals?limit=10", headers=_auth_headers(user["raw_api_key"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert len(body["withdrawals"]) == 1
    item = body["withdrawals"][0]
    assert item["wallet_id"] == wallet["wallet_id"]
    assert item["amount_cents"] == 1234
    assert item["status"] == "complete"


def test_outbound_url_validation_blocks_private_targets_by_default(client):
    user = _register_user()

    hook_resp = client.post(
        "/ops/jobs/hooks",
        headers=_auth_headers(user["raw_api_key"]),
        json={"target_url": "http://127.0.0.1:9999/hook"},
    )
    assert hook_resp.status_code == 422
    assert "private/loopback" in hook_resp.json()["message"]

    manifest_resp = client.post(
        "/onboarding/validate",
        headers=_auth_headers(user["raw_api_key"]),
        json={"manifest_url": "http://localhost:8000/agent.md"},
    )
    assert manifest_resp.status_code == 422
    assert "localhost" in manifest_resp.json()["message"]


def test_manifest_url_redirects_are_blocked(client, monkeypatch):
    user = _register_user()
    captured: dict[str, object] = {}

    class _RedirectResponse:
        status_code = 302
        headers = {"Location": "http://127.0.0.1/internal"}
        content = b""
        text = ""

        @staticmethod
        def raise_for_status():
            return None

    def _fake_get(url, timeout=None, allow_redirects=None):
        captured["url"] = url
        captured["allow_redirects"] = allow_redirects
        return _RedirectResponse()

    monkeypatch.setattr(server.http, "get", _fake_get)
    manifest_resp = client.post(
        "/onboarding/validate",
        headers=_auth_headers(user["raw_api_key"]),
        json={"manifest_url": "https://docs.example.com/agent.md"},
    )
    assert manifest_resp.status_code == 502
    assert "redirect" in manifest_resp.json()["message"].lower()
    assert captured["allow_redirects"] is False


def test_job_callback_url_delivery_is_signed_and_contains_terminal_output(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Callback Agent {uuid.uuid4().hex[:6]}",
        tags=["callback"],
    )

    callback_url = "https://hooks.example.com/job-callback"
    callback_secret = "super-secret-callback-key"
    callback_requests: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None, allow_redirects=None):
        callback_requests.append(
            {
                "url": url,
                "data": data,
                "headers": headers or {},
            }
        )
        resp = requests.Response()
        resp.status_code = 204
        resp._content = b""
        return resp

    monkeypatch.setattr(server.http, "post", fake_post)

    created = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "deliver callback"},
            "callback_url": callback_url,
            "callback_secret": callback_secret,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim.status_code == 200, claim.text

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "output_payload": {"ok": True, "source": "specialist"},
            "claim_token": claim.json()["claim_token"],
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "complete"

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
    assert payload["job_id"] == job_id
    assert payload["status"] == "complete"
    assert payload["output_payload"] == {"ok": True, "source": "specialist"}

    signature = callback_match["headers"].get("X-Aztea-Signature")
    expected_signature = "sha256=" + hmac.new(
        callback_secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected_signature


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

    def fake_post(url, data=None, headers=None, timeout=None, allow_redirects=None):
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

    timeout_job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    timeout_job_id = timeout_job["job_id"]
    claim = client.post(
        f"/jobs/{timeout_job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim.status_code == 200

    sla_job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=1)
    sla_job_id = sla_job["job_id"]
    retry_job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=3)
    retry_job_id = retry_job["job_id"]

    with jobs._conn() as conn:
        expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        retry_due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_expires_at = ? WHERE job_id = ?",
            (expired, timeout_job_id),
        )
        conn.execute(
            "UPDATE jobs SET created_at = ?, updated_at = ? WHERE job_id = ?",
            (old, old, sla_job_id),
        )
        conn.execute(
            """
            UPDATE jobs
            SET status = 'pending',
                next_retry_at = ?,
                last_retry_at = ?,
                claim_owner_id = ?,
                claim_token = ?,
                claimed_at = ?,
                lease_expires_at = ?,
                last_heartbeat_at = ?
            WHERE job_id = ?
            """,
            (
                retry_due,
                retry_due,
                f"user:{worker['user_id']}",
                "stale-claim-token",
                retry_due,
                retry_due,
                retry_due,
                retry_job_id,
            ),
        )

    sweep = client.post(
        "/ops/jobs/sweep",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"retry_delay_seconds": 0, "sla_seconds": 60, "limit": 100},
    )
    assert sweep.status_code == 200, sweep.text
    summary = sweep.json()
    assert timeout_job_id in summary["timeout_retry_job_ids"]
    assert timeout_job_id not in summary["timeout_failed_job_ids"]
    assert sla_job_id in summary["sla_failed_job_ids"]
    assert retry_job_id in summary["retry_ready_job_ids"]
    assert summary["retry_ready_count"] >= 1

    process = client.post(
        "/ops/jobs/hooks/process",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"limit": 200},
    )
    assert process.status_code == 200, process.text

    timeout_state = client.get(f"/jobs/{timeout_job_id}", headers=_auth_headers(caller["raw_api_key"]))
    sla_state = client.get(f"/jobs/{sla_job_id}", headers=_auth_headers(caller["raw_api_key"]))
    retry_state = client.get(f"/jobs/{retry_job_id}", headers=_auth_headers(caller["raw_api_key"]))
    assert timeout_state.status_code == 200
    assert sla_state.status_code == 200
    assert retry_state.status_code == 200
    assert timeout_state.json()["status"] == "pending"
    assert timeout_state.json()["next_retry_at"] is None
    assert sla_state.json()["status"] == "failed"
    assert retry_state.json()["status"] == "pending"
    assert retry_state.json()["next_retry_at"] is None
    assert retry_state.json()["last_retry_at"] is None
    assert retry_state.json()["claim_owner_id"] is None
    assert retry_state.json().get("claim_token") is None
    assert retry_state.json()["lease_expires_at"] is None
    assert retry_state.json()["last_heartbeat_at"] is None

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    assert (
        payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"]
        == 300 - int(timeout_job["caller_charge_cents"]) - int(retry_job["caller_charge_cents"])
    )

    events = client.get("/ops/jobs/events", headers=_auth_headers(caller["raw_api_key"]))
    assert events.status_code == 200
    event_types = {event["event_type"] for event in events.json()["events"]}
    assert "job.timeout_retry_scheduled" in event_types
    assert "job.sla_expired" in event_types
    assert "retry_ready" in event_types

    hook_event_types = {entry["payload"].get("event_type") for entry in hook_events}
    assert "job.timeout_retry_scheduled" in hook_event_types
    assert "job.sla_expired" in hook_event_types
    assert "retry_ready" in hook_event_types

    metrics = client.get("/ops/jobs/metrics", headers=_auth_headers(TEST_MASTER_KEY))
    assert metrics.status_code == 200
    body = metrics.json()
    assert "status_counts" in body
    assert "alerts" in body
    assert "hook_delivery" in body
    assert "slo" in body
    assert body["retry_ready_last_sweep"] >= 1


def test_hook_delivery_dead_letter_listing(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    monkeypatch.setattr(server, "_HOOK_DELIVERY_MAX_ATTEMPTS", 1)

    def always_fail_post(url, data=None, headers=None, timeout=None, allow_redirects=None):
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
    assert result["reason"] == "No output contract defined — structural check passed."


def test_builtin_worker_auto_completes_async_jobs(client, monkeypatch):
    monkeypatch.setattr(
        server.agent_textintel,
        "run",
        lambda text, mode: {
            "word_count": len(str(text).split()),
            "mode": mode,
            "summary": "processed by test builtin worker",
        },
    )

    master_wallet = payments.get_or_create_wallet("master")
    payments.deposit(master_wallet["wallet_id"], 500, "test builtin funds")

    created = client.post(
        "/jobs",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={
            "agent_id": server._TEXTINTEL_AGENT_ID,
            "input_payload": {"text": "hello world from async job", "mode": "quick"},
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
    assert terminal["output_payload"]["summary"] == "processed by test builtin worker"


def test_registry_lists_new_builtin_agents(client):
    listed = client.get("/registry/agents", headers=_auth_headers(TEST_MASTER_KEY))
    assert listed.status_code == 200, listed.text
    names = {agent["name"] for agent in listed.json()["agents"]}
    assert {
        "Negotiation Strategist Agent",
        "Scenario Simulator Agent",
        "Product Strategy Lab Agent",
        "Portfolio Planner Agent",
        "System Design Reviewer Agent",
        "Incident Response Commander Agent",
    }.issubset(names)


def test_registry_hides_deprecated_builtin_agents(client):
    listed = client.get("/registry/agents", headers=_auth_headers(TEST_MASTER_KEY))
    assert listed.status_code == 200, listed.text
    names = {agent["name"] for agent in listed.json()["agents"]}
    assert "Resume Analyzer Agent" not in names
    assert "Email Sequence Writer Agent" not in names


def test_builtin_agents_registered_to_system_owner_with_internal_endpoints(client):
    with auth._conn() as conn:
        system_row = conn.execute(
            "SELECT user_id, status FROM users WHERE username = ? LIMIT 1",
            ("system",),
        ).fetchone()
    assert system_row is not None
    assert str(system_row["status"]).lower() == "suspended"
    system_owner = f"user:{system_row['user_id']}"

    for builtin_id in (
        server._FINANCIAL_AGENT_ID,
        server._CODEREVIEW_AGENT_ID,
        server._TEXTINTEL_AGENT_ID,
        server._WIKI_AGENT_ID,
        server._NEGOTIATION_AGENT_ID,
        server._SCENARIO_AGENT_ID,
        server._PRODUCT_AGENT_ID,
        server._PORTFOLIO_AGENT_ID,
        server._QUALITY_JUDGE_AGENT_ID,
    ):
        agent = registry.get_agent(builtin_id)
        assert agent is not None
        assert agent["owner_id"] == system_owner
        assert str(agent["endpoint_url"]).startswith("internal://")
        assert float(agent["price_per_call_usd"]) == pytest.approx(0.01)
        assert isinstance(agent.get("output_examples"), list)
        assert len(agent["output_examples"]) >= 1


def test_registry_call_routes_internal_builtin_without_http_and_records_job(client, monkeypatch):
    caller = _register_user()
    _fund_user_wallet(caller, 100)

    monkeypatch.setattr(
        server.agent_textintel,
        "run",
        lambda text, mode: {"summary": f"internal::{mode}", "word_count": len(str(text).split())},
    )

    def _fail_post(*args, **kwargs):
        raise AssertionError("registry_call should not use outbound HTTP for internal:// endpoints")

    monkeypatch.setattr(server.http, "post", _fail_post)

    call = client.post(
        f"/registry/agents/{server._TEXTINTEL_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"text": "hello from internal route", "mode": "quick"},
    )
    assert call.status_code == 200, call.text
    assert call.json()["summary"] == "internal::quick"

    caller_owner = f"user:{caller['user_id']}"
    jobs_for_caller = jobs.list_jobs_for_owner(caller_owner, limit=20)
    synced = [item for item in jobs_for_caller if item["agent_id"] == server._TEXTINTEL_AGENT_ID]
    assert synced
    assert synced[0]["status"] == "complete"
    assert synced[0]["output_payload"]["summary"] == "internal::quick"
    settled = _force_settle_completed_job(synced[0]["job_id"])
    assert settled["settled_at"] is not None

    caller_wallet = payments.get_or_create_wallet(caller_owner)
    agent_wallet = payments.get_or_create_wallet(f"agent:{server._TEXTINTEL_AGENT_ID}")
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 99
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] >= 1


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
        server.agent_textintel,
        "run",
        lambda text, mode: {"summary": f"mcp::{mode}", "word_count": len(str(text).split())},
    )

    response = client.post(
        "/mcp/invoke",
        json={
            "tool_name": "text_intelligence_agent",
            "input": {"text": "mcp invoke payload", "mode": "quick"},
            "api_key": caller["raw_api_key"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body.get("content"), list) and body["content"]
    assert body["content"][0]["type"] == "text"
    invoked_payload = json.loads(body["content"][0]["text"])
    assert invoked_payload["summary"] == "mcp::quick"

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 99


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
    assert manifest["name"] == "agentmarket"
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
            "description": "awaiting review",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.10,
            "tags": ["pending-review"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
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
            "description": "awaiting review",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.10,
            "tags": ["pending-hidden"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
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
            "description": "awaiting review",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.10,
            "tags": ["pending-claim"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
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
            "description": "awaiting review",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "healthcheck_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}/health",
            "price_per_call_usd": 0.10,
            "tags": ["review-flow"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
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
            "description": "awaiting review",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.10,
            "tags": ["review-reject"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
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
    builtin = registry.get_agent(server._FINANCIAL_AGENT_ID, include_unapproved=True)
    assert builtin is not None
    assert builtin["review_status"] == "approved"


def test_protocol_version_header_is_always_set(client):
    response = client.get("/health", headers=_auth_headers(TEST_MASTER_KEY))
    assert response.status_code == 200
    assert response.headers.get("X-Aztea-Version") == "1.0"


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


def test_public_docs_index_and_content_are_available_without_auth(client):
    index_response = client.get("/public/docs")
    assert index_response.status_code == 200, index_response.text
    body = index_response.json()
    assert body["count"] >= 1
    assert body["docs"]

    first = body["docs"][0]
    assert first["path"] == f"/public/docs/{first['slug']}"

    doc_response = client.get(first["path"])
    assert doc_response.status_code == 200, doc_response.text
    doc_body = doc_response.json()
    assert doc_body["slug"] == first["slug"]
    assert isinstance(doc_body["content"], str)
    assert doc_body["content"].strip()


def test_public_docs_unknown_slug_returns_404(client):
    response = client.get("/public/docs/not-a-real-doc")
    assert response.status_code == 404
