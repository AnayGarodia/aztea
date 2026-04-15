import os

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

import hashlib
import json
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 190
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 9
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
    assert body["status"] == "failed"
    assert body["timeout_count"] == 1
    assert body["error_message"] == "Job lease expired before completion."
    assert body["claim_owner_id"] is None


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

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 190
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 9
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

    mapped_legacy = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "legacy-custom-message", "payload": {"text": "still works"}},
    )
    assert mapped_legacy.status_code == 201, mapped_legacy.text
    assert mapped_legacy.json()["type"] == "note"
    assert mapped_legacy.json()["payload"]["text"] == "still works"


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
    assert "private/loopback" in hook_resp.json()["message"]

    manifest_resp = client.post(
        "/onboarding/validate",
        headers=_auth_headers(user["raw_api_key"]),
        json={"manifest_url": "http://localhost:8000/agent.md"},
    )
    assert manifest_resp.status_code == 422
    assert "localhost" in manifest_resp.json()["message"]


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
    assert timeout_job_id in summary["timeout_failed_job_ids"]
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
    assert timeout_state.json()["status"] == "failed"
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
        == 300 - int(retry_job["price_cents"])
    )

    events = client.get("/ops/jobs/events", headers=_auth_headers(caller["raw_api_key"]))
    assert events.status_code == 200
    event_types = {event["event_type"] for event in events.json()["events"]}
    assert "job.timeout_terminal" in event_types
    assert "job.sla_expired" in event_types
    assert "retry_ready" in event_types

    hook_event_types = {entry["payload"].get("event_type") for entry in hook_events}
    assert "job.timeout_terminal" in hook_event_types
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
    }.issubset(names)


def test_mcp_tools_manifest_exposes_registered_agent_schema(client):
    owner = _register_user()
    agent_name = f"MCP Tool Agent {uuid.uuid4().hex[:6]}"
    response = client.post(
        "/registry/register",
        headers=_auth_headers(owner["raw_api_key"]),
        json={
            "name": agent_name,
            "description": "MCP manifest integration test agent.",
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
    tool = next((item for item in body["tools"] if agent_name in item["description"]), None)
    assert tool is not None
    assert tool["name"].startswith("agentmarket__")
    assert tool["inputSchema"]["properties"]["task"]["type"] == "string"
    assert "task" in tool["inputSchema"].get("required", [])
    assert tool["outputSchema"]["properties"]["result"]["type"] == "string"


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
    assert body["error"] == "SCHEMA_MISMATCH"
    assert body["data"]["mismatches"]


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
    agent_key = key_resp.json()["raw_key"]

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
    assert blocked.json()["error"] == "AGENT_SUSPENDED"

    active = registry.set_agent_status(agent_id, "active")
    assert active is not None
    pending = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 290

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
    assert dispute.json()["error"] == "DISPUTE_WINDOW_CLOSED"


def test_protocol_version_header_is_always_set(client):
    response = client.get("/health", headers=_auth_headers(TEST_MASTER_KEY))
    assert response.status_code == 200
    assert response.headers.get("X-AgentMarket-Version") == "1.0"
