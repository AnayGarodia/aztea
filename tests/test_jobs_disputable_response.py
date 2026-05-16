"""Server-side tests for the `disputable` annotation on `_job_response`
and the comma-list `status` filter on `GET /jobs`.

These tests use the same DB-isolated TestClient pattern as
`tests/test_disputes.py` so each test gets a clean SQLite file.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
# Setup helpers (mirror tests/test_disputes.py)
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
            "description": "disputable annotation test",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.05,
            "tags": ["dispute-annotate"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "title": "Task",
                        "description": "test input",
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


def _fail_job(client: TestClient, worker_key: str, job_id: str) -> None:
    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker_key),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    token = claim.json()["claim_token"]
    fail = client.post(
        f"/jobs/{job_id}/fail",
        headers=_auth_headers(worker_key),
        json={"reason": "test", "claim_token": token},
    )
    assert fail.status_code == 200, fail.text


@pytest.fixture
def client(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-disputable-{uuid.uuid4().hex}.db"
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
    """Returns dict with caller / worker users + raw API keys + agent_id."""
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


# ---------------------------------------------------------------------------
# `disputable` annotations on the job response
# ---------------------------------------------------------------------------


def test_jobs_list_includes_disputable_fields(client, setup) -> None:
    job = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], job["job_id"])
    resp = client.get("/jobs", headers=_auth_headers(setup["caller_key"]))
    assert resp.status_code == 200, resp.text
    items = resp.json()["jobs"]
    assert items, "Caller should see their completed job"
    target = next((j for j in items if j["job_id"] == job["job_id"]), None)
    assert target is not None
    for key in ("disputable", "disputable_reason", "disputable_code"):
        assert key in target, f"Job response missing {key}"


def test_eligible_job_marked_disputable(client, setup) -> None:
    job = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], job["job_id"])
    resp = client.get(
        f"/jobs/{job['job_id']}", headers=_auth_headers(setup["caller_key"])
    )
    body = resp.json()
    assert body["disputable"] is True
    assert body["disputable_reason"] is None
    assert body["disputable_code"] is None


def test_pending_job_not_disputable(client, setup) -> None:
    job = _create_job(client, setup["caller_key"], setup["agent_id"])
    # Don't complete — leave in pending.
    resp = client.get(
        f"/jobs/{job['job_id']}", headers=_auth_headers(setup["caller_key"])
    )
    body = resp.json()
    assert body["disputable"] is False
    assert body["disputable_code"] == "dispute.not_completed"


def test_already_disputed_job_marked_ineligible(client, setup) -> None:
    job = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], job["job_id"])
    # File a dispute.
    file_resp = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(setup["caller_key"]),
        json={"reason": "test dispute reason"},
    )
    assert file_resp.status_code == 201, file_resp.text
    # Re-fetch; disputable should now be False with the right code.
    resp = client.get(
        f"/jobs/{job['job_id']}", headers=_auth_headers(setup["caller_key"])
    )
    body = resp.json()
    assert body["disputable"] is False
    assert body["disputable_code"] == "dispute.already_filed"


def test_already_rated_job_marked_ineligible(client, setup) -> None:
    job = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], job["job_id"])
    # Submit a rating before disputing.
    rate_resp = client.post(
        f"/jobs/{job['job_id']}/rating",
        headers=_auth_headers(setup["caller_key"]),
        json={"rating": 3},
    )
    assert rate_resp.status_code == 201, rate_resp.text
    resp = client.get(
        f"/jobs/{job['job_id']}", headers=_auth_headers(setup["caller_key"])
    )
    body = resp.json()
    assert body["disputable"] is False
    assert body["disputable_code"] == "dispute.already_rated"


def test_window_expired_job_marked_ineligible(client, setup, monkeypatch) -> None:
    job = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], job["job_id"])
    # Manually rewind completed_at to the distant past so the window expires.
    very_old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    with payments._conn() as conn:
        conn.execute(
            "UPDATE jobs SET completed_at = %s WHERE job_id = %s",
            (very_old, job["job_id"]),
        )
        conn.commit()
    resp = client.get(
        f"/jobs/{job['job_id']}", headers=_auth_headers(setup["caller_key"])
    )
    body = resp.json()
    assert body["disputable"] is False
    assert body["disputable_code"] == "dispute.window_closed"


def test_master_caller_response_excludes_disputable_fields(client, setup) -> None:
    job = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], job["job_id"])
    resp = client.get(
        f"/jobs/{job['job_id']}", headers=_auth_headers(TEST_MASTER_KEY)
    )
    body = resp.json()
    # Master view returns the raw dict with no disputability annotation.
    assert "disputable" not in body


# ---------------------------------------------------------------------------
# `GET /jobs?status=` comma-list filter
# ---------------------------------------------------------------------------


def test_jobs_list_single_status_unchanged(client, setup) -> None:
    job_a = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], job_a["job_id"])
    job_b = _create_job(client, setup["caller_key"], setup["agent_id"])
    _fail_job(client, setup["worker_key"], job_b["job_id"])

    resp = client.get(
        "/jobs?status=complete", headers=_auth_headers(setup["caller_key"])
    )
    assert resp.status_code == 200
    items = resp.json()["jobs"]
    assert all(j["status"] == "complete" for j in items)


def test_jobs_list_comma_status_returns_both(client, setup) -> None:
    completed = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], completed["job_id"])
    failed = _create_job(client, setup["caller_key"], setup["agent_id"])
    _fail_job(client, setup["worker_key"], failed["job_id"])

    resp = client.get(
        "/jobs?status=complete,failed", headers=_auth_headers(setup["caller_key"])
    )
    assert resp.status_code == 200
    items = resp.json()["jobs"]
    statuses = {j["status"] for j in items}
    assert "complete" in statuses
    assert "failed" in statuses


def test_jobs_list_comma_status_excludes_pending(client, setup) -> None:
    pending = _create_job(client, setup["caller_key"], setup["agent_id"])
    completed = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], completed["job_id"])

    resp = client.get(
        "/jobs?status=complete,failed", headers=_auth_headers(setup["caller_key"])
    )
    items = resp.json()["jobs"]
    assert all(j["status"] != "pending" for j in items)
    assert pending["job_id"] not in {j["job_id"] for j in items}


def test_jobs_list_comma_status_invalid_member_422(client, setup) -> None:
    resp = client.get(
        "/jobs?status=complete,bogus", headers=_auth_headers(setup["caller_key"])
    )
    assert resp.status_code == 422
    assert "bogus" in resp.text


def test_jobs_list_comma_status_dedupes(client, setup) -> None:
    """Same status repeated twice in the comma list — no duplicate rows."""
    job = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], job["job_id"])
    resp = client.get(
        "/jobs?status=complete,complete",
        headers=_auth_headers(setup["caller_key"]),
    )
    items = resp.json()["jobs"]
    job_ids = [j["job_id"] for j in items]
    assert len(job_ids) == len(set(job_ids))


def test_jobs_list_comma_status_sorted_by_created_at_desc(client, setup) -> None:
    j1 = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], j1["job_id"])
    j2 = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], j2["job_id"])
    resp = client.get(
        "/jobs?status=complete,failed",
        headers=_auth_headers(setup["caller_key"]),
    )
    items = resp.json()["jobs"]
    assert items[0]["job_id"] == j2["job_id"], "Newer job should appear first"


def test_jobs_list_unknown_status_alone_422(client, setup) -> None:
    resp = client.get(
        "/jobs?status=bogus", headers=_auth_headers(setup["caller_key"])
    )
    assert resp.status_code == 422


def test_jobs_list_no_status_filter_returns_all(client, setup) -> None:
    j_complete = _create_job(client, setup["caller_key"], setup["agent_id"])
    _complete_job(client, setup["worker_key"], j_complete["job_id"])
    j_pending = _create_job(client, setup["caller_key"], setup["agent_id"])
    resp = client.get("/jobs", headers=_auth_headers(setup["caller_key"]))
    items = resp.json()["jobs"]
    job_ids = {j["job_id"] for j in items}
    assert j_complete["job_id"] in job_ids
    assert j_pending["job_id"] in job_ids
