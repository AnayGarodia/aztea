import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core import auth
from core import disputes
from core import jobs
from core import judges
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
    input_schema: dict | None = None,
) -> str:
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(raw_api_key),
        json={
            "name": name,
            "description": "dispute test agent",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": price,
            "tags": ["dispute-test"],
            "input_schema": input_schema or {"type": "object", "properties": {"task": {"type": "string"}}},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["agent_id"])


def _fund_user_wallet(user: dict, amount_cents: int = 200) -> dict:
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")
    payments.deposit(wallet["wallet_id"], amount_cents, "dispute test funds")
    return wallet


def _create_job_via_api(client: TestClient, raw_api_key: str, *, agent_id: str) -> dict:
    resp = client.post(
        "/jobs",
        headers=_auth_headers(raw_api_key),
        json={"agent_id": agent_id, "input_payload": {"task": "analyze"}, "max_attempts": 2},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _complete_job(client: TestClient, worker_key: str, job_id: str) -> dict:
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
    return complete.json()


def _wallet_balance(owner_id: str) -> int:
    wallet = payments.get_or_create_wallet(owner_id)
    latest = payments.get_wallet(wallet["wallet_id"])
    assert latest is not None
    return int(latest["balance_cents"])


@pytest.fixture
def client(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-disputes-{uuid.uuid4().hex}.db"
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


def test_dispute_consensus_caller_wins_full_refund(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    agent_id = _register_agent_via_api(client, worker["raw_api_key"], name=f"Consensus Agent {uuid.uuid4().hex[:6]}")

    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    _complete_job(client, worker["raw_api_key"], job["job_id"])

    caller_owner = f"user:{caller['user_id']}"
    assert _wallet_balance(caller_owner) == 190
    assert _wallet_balance(f"agent:{agent_id}") == 9
    assert _wallet_balance(payments.PLATFORM_OWNER_ID) == 1

    filed = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"reason": "Output is incomplete", "evidence": "section 7 omitted"},
    )
    assert filed.status_code == 201, filed.text
    dispute_id = filed.json()["dispute_id"]

    assert _wallet_balance(f"agent:{agent_id}") == 0
    assert _wallet_balance(payments.PLATFORM_OWNER_ID) == 0

    def _consensus(dispute_id_arg: str) -> dict:
        disputes.record_judgment(
            dispute_id_arg,
            judge_kind="llm_primary",
            verdict="caller_wins",
            reasoning="Caller evidence is stronger.",
            model="m1",
        )
        disputes.record_judgment(
            dispute_id_arg,
            judge_kind="llm_secondary",
            verdict="caller_wins",
            reasoning="Caller evidence is stronger.",
            model="m2",
        )
        disputes.set_dispute_consensus(dispute_id_arg, "caller_wins")
        return {"status": "consensus", "outcome": "caller_wins", "judgments": disputes.get_judgments(dispute_id_arg)}

    monkeypatch.setattr(judges, "run_judgment", _consensus)
    judged = client.post(
        f"/ops/disputes/{dispute_id}/judge",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert judged.status_code == 200, judged.text
    assert judged.json()["dispute"]["status"] == "resolved"
    assert judged.json()["dispute"]["outcome"] == "caller_wins"
    assert _wallet_balance(caller_owner) == 200


def test_dispute_tie_then_admin_split_settlement(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    agent_id = _register_agent_via_api(client, worker["raw_api_key"], name=f"Tie Agent {uuid.uuid4().hex[:6]}")

    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    _complete_job(client, worker["raw_api_key"], job["job_id"])

    filed = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"reason": "Caller changed requirements mid-stream"},
    )
    assert filed.status_code == 201, filed.text
    dispute_id = filed.json()["dispute_id"]

    def _tie(dispute_id_arg: str) -> dict:
        disputes.record_judgment(
            dispute_id_arg,
            judge_kind="llm_primary",
            verdict="caller_wins",
            reasoning="Primary judge favors caller.",
            model="m1",
        )
        disputes.record_judgment(
            dispute_id_arg,
            judge_kind="llm_secondary",
            verdict="agent_wins",
            reasoning="Secondary judge favors agent.",
            model="m2",
        )
        disputes.set_dispute_status(dispute_id_arg, "tied")
        return {"status": "tied", "outcome": None, "judgments": disputes.get_judgments(dispute_id_arg)}

    monkeypatch.setattr(judges, "run_judgment", _tie)
    judged = client.post(
        f"/ops/disputes/{dispute_id}/judge",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert judged.status_code == 200, judged.text
    assert judged.json()["dispute"]["status"] == "tied"

    ruled = client.post(
        f"/admin/disputes/{dispute_id}/rule",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={
            "outcome": "split",
            "split_caller_cents": 6,
            "split_agent_cents": 4,
            "reasoning": "Both parties partially met obligations.",
        },
    )
    assert ruled.status_code == 200, ruled.text
    body = ruled.json()
    assert body["dispute"]["status"] == "final"
    assert body["dispute"]["outcome"] == "split"

    caller_owner = f"user:{caller['user_id']}"
    assert _wallet_balance(caller_owner) == 196
    assert _wallet_balance(f"agent:{agent_id}") == 4
    assert _wallet_balance(payments.PLATFORM_OWNER_ID) == 0


def test_double_file_and_rate_after_dispute_blocked(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    agent_id = _register_agent_via_api(client, worker["raw_api_key"], name=f"Lock Agent {uuid.uuid4().hex[:6]}")
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    _complete_job(client, worker["raw_api_key"], job["job_id"])

    first = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"reason": "Initial dispute"},
    )
    assert first.status_code == 201, first.text

    second = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"reason": "Duplicate dispute"},
    )
    assert second.status_code == 409

    caller_rating = client.post(
        f"/jobs/{job['job_id']}/rating",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"rating": 5},
    )
    assert caller_rating.status_code == 409

    worker_rating = client.post(
        f"/jobs/{job['job_id']}/rate-caller",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"rating": 2, "comment": "Late feedback"},
    )
    assert worker_rating.status_code == 400 or worker_rating.status_code == 409


def test_min_caller_trust_gates_job_creation(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Gated Agent {uuid.uuid4().hex[:6]}",
        input_schema={
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "min_caller_trust": 0.7,
        },
    )

    create = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "x"}, "max_attempts": 2},
    )
    assert create.status_code == 403, create.text
    error = create.json()
    assert error["error"] == "auth.forbidden"


def test_clawback_moves_settled_payout_into_escrow(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    agent_id = _register_agent_via_api(client, worker["raw_api_key"], name=f"Clawback Agent {uuid.uuid4().hex[:6]}")

    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    _complete_job(client, worker["raw_api_key"], job["job_id"])

    assert _wallet_balance(f"agent:{agent_id}") == 9
    assert _wallet_balance(payments.PLATFORM_OWNER_ID) == 1

    filed = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"reason": "Need review"},
    )
    assert filed.status_code == 201, filed.text
    dispute_id = filed.json()["dispute_id"]
    escrow_wallet = payments.get_or_create_wallet(f"{payments.DISPUTE_ESCROW_OWNER_PREFIX}{dispute_id}")
    assert _wallet_balance(escrow_wallet["owner_id"]) == 10
    assert _wallet_balance(f"agent:{agent_id}") == 0
    assert _wallet_balance(payments.PLATFORM_OWNER_ID) == 0


def test_dispute_filing_rolls_back_when_clawback_lock_fails(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    agent_id = _register_agent_via_api(client, worker["raw_api_key"], name=f"Rollback Agent {uuid.uuid4().hex[:6]}")

    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    _complete_job(client, worker["raw_api_key"], job["job_id"])
    job_row = jobs.get_job(job["job_id"])
    assert job_row is not None

    with payments._conn() as conn:
        conn.execute(
            "UPDATE wallets SET balance_cents = 0 WHERE wallet_id IN (?, ?)",
            (job_row["agent_wallet_id"], job_row["platform_wallet_id"]),
        )

    filed = client.post(
        f"/jobs/{job['job_id']}/dispute",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"reason": "force clawback failure"},
    )
    assert filed.status_code == 409, filed.text
    assert filed.json()["error"] == "dispute.clawback_insufficient_balance"
    assert disputes.get_dispute_by_job(job["job_id"]) is None
