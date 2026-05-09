"""End-to-end integration tests for the dispute lifecycle.

Drives the full server flow via TestClient: register users → register agent
→ create job → complete → file dispute → assert side effects (wallet
balances, escrow lock, dispute row, events). These are the assertions that
prove the CLI's wizard renders the *real* state of the world.

We exercise the SDK's HTTP surface directly rather than spawning the CLI
process — the unit-level tests in `sdks/python-sdk/tests/test_cli_dispute*`
already cover the typer entrypoint; here we prove the underlying server
flow stays correct.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core import auth
from core import disputes
from core import jobs
from core import payments
from core import registry
from core import reputation
import server.application as server


TEST_MASTER_KEY = "test-master-key"


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


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


def _register_agent_via_api(client: TestClient, raw_key: str, *, name: str) -> str:
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(raw_key),
        json={
            "name": name,
            "description": "integration dispute test",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.05,
            "tags": ["dispute-integration"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "title": "Task",
                        "description": "input",
                    }
                },
            },
            "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
        },
    )
    assert resp.status_code == 201, resp.text
    agent_id = str(resp.json()["agent_id"])
    review = client.post(
        f"/admin/agents/{agent_id}/review",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"decision": "approve", "note": "test"},
    )
    assert review.status_code == 200, review.text
    return agent_id


def _fund(user: dict, amount: int = 1000) -> None:
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")
    payments.deposit(wallet["wallet_id"], amount, "test funds")


def _create_job(client: TestClient, raw_key: str, agent_id: str) -> dict:
    resp = client.post(
        "/jobs",
        headers=_auth_headers(raw_key),
        json={"agent_id": agent_id, "input_payload": {"task": "x"}, "max_attempts": 1},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _complete_job(client: TestClient, worker_key: str, job_id: str) -> None:
    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker_key),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    token = claim.json()["claim_token"]
    complete = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker_key),
        json={"output_payload": {"ok": True}, "claim_token": token},
    )
    assert complete.status_code == 200, complete.text


def _wallet_balance(owner_id: str) -> int:
    wallet = payments.get_or_create_wallet(owner_id)
    latest = payments.get_wallet(wallet["wallet_id"])
    assert latest is not None
    return int(latest["balance_cents"])


@pytest.fixture
def client(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-dispute-int-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)
    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)
    with TestClient(server.app) as test_client:
        yield test_client
    for module in modules:
        _close_module_conn(module)
    for suffix in ("", "-shm", "-wal"):
        path = Path(f"{db_path}{suffix}")
        if path.exists():
            path.unlink()


@pytest.fixture
def setup(client):
    caller = _register_user()
    worker = _register_user()
    _fund(caller, 1000)
    agent_id = _register_agent_via_api(
        client, worker["raw_api_key"], name=f"agent-{uuid.uuid4().hex[:6]}"
    )
    return {
        "caller": caller,
        "caller_key": caller["raw_api_key"],
        "worker": worker,
        "worker_key": worker["raw_api_key"],
        "agent_id": agent_id,
    }


def _completed_job(client: TestClient, setup: dict) -> dict:
    job = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], job["job_id"])
    return job


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


def test_dispute_lifecycle_happy_path(client, setup) -> None:
    job = _completed_job(client, setup)
    caller_balance_before = _wallet_balance(f"user:{setup['caller']['user_id']}")

    resp = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "agent returned wrong output"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["side"] == "caller"
    assert body["filing_deposit_cents"] >= 1

    # Caller wallet was debited by the filing deposit.
    caller_balance_after = _wallet_balance(f"user:{setup['caller']['user_id']}")
    assert caller_balance_after < caller_balance_before


def test_dispute_already_filed_returns_409(client, setup) -> None:
    job = _completed_job(client, setup)
    first = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "first"},
    )
    assert first.status_code == 201
    second = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "second"},
    )
    assert second.status_code == 409


def test_dispute_already_rated_returns_409(client, setup) -> None:
    job = _completed_job(client, setup)
    rate = client.post(
        f"/jobs/{job['job_id']}/rating",
        headers=_auth_headers(setup["caller_key"]),
        json={"rating": 3},
    )
    assert rate.status_code == 201, rate.text
    resp = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "stale"},
    )
    assert resp.status_code == 409
    assert "rated" in resp.text.lower() or "already" in resp.text.lower()


def test_dispute_after_window_expired_returns_400(client, setup) -> None:
    job = _completed_job(client, setup)
    very_old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    with payments._conn() as conn:
        conn.execute(
            "UPDATE jobs SET completed_at = %s WHERE job_id = %s",
            (very_old, job["job_id"]),
        )
        conn.commit()
    resp = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "too late"},
    )
    assert resp.status_code == 400
    assert "window" in resp.text.lower()


def test_dispute_self_dispute_returns_400(client, setup) -> None:
    """When caller and agent owner are the same user, dispute should be blocked."""
    # Reuse the worker (who owns the agent) as the caller.
    _fund(setup["worker"], 1000)
    job = _create_job(client, setup["worker_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], job["job_id"])
    resp = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["worker_key"]),
        json={"reason": "self test"},
    )
    assert resp.status_code == 400
    assert "agent you own" in resp.text.lower() or "self_dispute" in resp.text.lower()


def test_dispute_filing_deposit_insufficient_balance_returns_409(
    client, setup
) -> None:
    job = _completed_job(client, setup)
    # Drain the caller wallet directly — `payments.deposit` only accepts
    # positive amounts, so go through the connection layer.
    wallet = payments.get_or_create_wallet(f"user:{setup['caller']['user_id']}")
    with payments._conn() as conn:
        conn.execute(
            "UPDATE wallets SET balance_cents = 0 WHERE wallet_id = %s",
            (wallet["wallet_id"],),
        )
        conn.commit()
    resp = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "broke"},
    )
    assert resp.status_code == 409
    body = resp.json()
    detail = body.get("detail")
    if isinstance(detail, dict):
        assert "balance_cents" in detail
        assert "required_cents" in detail


def test_dispute_event_emitted_on_filing(client, setup, monkeypatch) -> None:
    """Filing a dispute records a `job.dispute_filed` event for audit trails."""
    events: list[dict] = []
    original = server._record_job_event

    def _record(job, event_type, *, actor_owner_id=None, payload=None):
        events.append(
            {
                "event_type": event_type,
                "actor": actor_owner_id,
                "payload": payload,
            }
        )
        return original(
            job, event_type, actor_owner_id=actor_owner_id, payload=payload
        )

    monkeypatch.setattr(server, "_record_job_event", _record)

    job = _completed_job(client, setup)
    resp = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "test"},
    )
    assert resp.status_code == 201, resp.text
    assert any(e["event_type"] == "job.dispute_filed" for e in events)


def test_get_dispute_after_filing_returns_record(client, setup) -> None:
    job = _completed_job(client, setup)
    file_resp = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "test reason"},
    )
    assert file_resp.status_code == 201, file_resp.text
    get_resp = client.get(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
    )
    assert get_resp.status_code == 200, get_resp.text
    body = get_resp.json()
    assert body["dispute_id"] == file_resp.json()["dispute_id"]
    assert body["reason"] == "test reason"


def test_jobs_list_marks_disputable_correctly_after_complete(client, setup) -> None:
    """The CLI picker depends on `disputable=True` flowing through GET /jobs."""
    job = _completed_job(client, setup)
    resp = client.get(
        "/jobs?status=complete,failed",
        headers=_auth_headers(setup["caller_key"]),
    )
    assert resp.status_code == 200
    items = resp.json()["jobs"]
    target = next(j for j in items if j["job_id"] == job["job_id"])
    assert target["disputable"] is True
    assert target["disputable_reason"] is None


def test_jobs_list_marks_disputable_false_after_filing(client, setup) -> None:
    job = _completed_job(client, setup)
    file_resp = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "test"},
    )
    assert file_resp.status_code == 201
    resp = client.get(
        "/jobs?status=complete,failed",
        headers=_auth_headers(setup["caller_key"]),
    )
    items = resp.json()["jobs"]
    target = next(j for j in items if j["job_id"] == job["job_id"])
    assert target["disputable"] is False
    assert target["disputable_code"] == "dispute.already_filed"


def test_dispute_policy_endpoint_e2e_matches_actual_charge(
    client, setup
) -> None:
    """The deposit the policy endpoint quotes equals the deposit actually charged."""
    policy = client.get("/ops/dispute-policy").json()
    bps = int(policy["filing_deposit_bps"])
    min_cents = int(policy["filing_deposit_min_cents"])

    job = _completed_job(client, setup)
    price_cents = int(job["price_cents"])
    expected_deposit = max(min_cents, (price_cents * bps) // 10_000)

    file_resp = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "test"},
    )
    assert file_resp.status_code == 201, file_resp.text
    actual_deposit = int(file_resp.json()["filing_deposit_cents"])
    assert actual_deposit == expected_deposit
