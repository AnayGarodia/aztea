"""Tests for /admin/usage/{digest,inspect,query} — the observability surface.

Each view runs against a small seeded SQLite DB so we can assert the shape and
non-empty cases without spinning up the full app. The router itself is the
real production code path; only the auth helpers and DB connection are
fixtured.
"""

from __future__ import annotations

import json
import os
import uuid
import sqlite3
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core import auth, jobs, payments, registry, reputation, disputes
from core import db as _db
from core.migrate import apply_migrations
import server.application as server


TEST_MASTER_KEY = "test-master-key"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_MASTER_KEY}"}


def _close_module_conn(module) -> None:
    conn = getattr(module._local, "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(module._local, "conn")
    except AttributeError:
        pass


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_jobs(db_path: str, rows: list[dict]) -> None:
    """Insert minimal job rows directly; bypasses charge logic for test setup."""
    conn = sqlite3.connect(db_path)
    for r in rows:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, agent_id, agent_owner_id, caller_owner_id,
                caller_wallet_id, agent_wallet_id, platform_wallet_id,
                status, price_cents, caller_charge_cents, charge_tx_id,
                input_payload, created_at, updated_at, origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["job_id"], r["agent_id"], "owner:agent", r["caller"],
                "w:caller", "w:agent", "w:platform",
                r["status"], 100, 110, "tx-" + uuid.uuid4().hex[:8],
                "{}", r["created_at"], r["created_at"], r.get("origin"),
            ),
        )
    conn.commit()
    conn.close()


def _seed_decisions(db_path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    for r in rows:
        intent = r["intent_text"]
        conn.execute(
            """
            INSERT INTO auto_hire_decisions (
                decision_id, caller_owner_id, caller_key_id, intent_text,
                intent_hash, auto_invoked, dry_run, reason, chosen_agent_id,
                confidence, candidates_json, resulting_job_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.get("decision_id", uuid.uuid4().hex),
                r.get("caller_owner_id"),
                r.get("caller_key_id"),
                intent,
                hashlib.sha256(intent.encode()).hexdigest(),
                int(r.get("auto_invoked", 0)),
                int(r.get("dry_run", 0)),
                r.get("reason"),
                r.get("chosen_agent_id"),
                r.get("confidence"),
                json.dumps(r.get("candidates", [])),
                r.get("resulting_job_id"),
                r["created_at"],
            ),
        )
    conn.commit()
    conn.close()


def _seed_mcp_log(db_path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    for r in rows:
        conn.execute(
            """
            INSERT INTO mcp_invocation_log
                (id, agent_id, caller_key_id, tool_name, input_hash,
                 invoked_at, duration_ms, success, error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex, r["agent_id"], r.get("caller_key_id", "k1"),
                r["tool_name"], "h" + uuid.uuid4().hex[:8],
                r["invoked_at"], r.get("duration_ms", 50),
                int(r.get("success", 1)), r.get("error_code"),
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def client(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-admin-usage-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)
    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))
    monkeypatch.setattr(_db, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)
    # Apply the full migration set so 0047+ tables exist before the router runs.
    apply_migrations(str(db_path))
    with TestClient(server.app) as test_client:
        yield (test_client, str(db_path))
    for module in modules:
        _close_module_conn(module)
    for suffix in ("", "-shm", "-wal"):
        path = Path(f"{db_path}{suffix}")
        if path.exists():
            path.unlink()


# ── Digest ─────────────────────────────────────────────────────────────────


def test_digest_returns_full_shape_with_empty_db(client):
    test_client, _db = client
    resp = test_client.get("/admin/usage/digest?window=24h", headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Every documented top-level section must be present.
    for key in ("window", "as_of", "calls", "spend", "top_agents",
                "failing_agents", "users", "auto_hire"):
        assert key in body, f"missing section {key!r}: {body}"
    assert body["window"] == "24h"
    assert body["calls"]["total"]["value"] == 0
    assert body["calls"]["total"]["delta_pct"] is None  # empty current + empty prior
    assert body["top_agents"] == []
    assert body["auto_hire"]["no_match"] == 0


def test_digest_counts_recent_jobs_and_computes_trend(client):
    test_client, db_path = client
    now = _now_iso()
    _seed_jobs(db_path, [
        {"job_id": "j1", "agent_id": "a1", "caller": "user:u1", "status": "complete",
         "created_at": now, "origin": "direct"},
        {"job_id": "j2", "agent_id": "a1", "caller": "user:u1", "status": "complete",
         "created_at": now, "origin": "auto_hire"},
        {"job_id": "j3", "agent_id": "a1", "caller": "user:u2", "status": "failed",
         "created_at": now, "origin": "direct"},
    ])
    resp = test_client.get("/admin/usage/digest?window=24h", headers=AUTH_HEADERS)
    body = resp.json()
    assert body["calls"]["total"]["value"] == 3
    assert body["calls"]["success"] == 2
    assert body["calls"]["failed"] == 1
    assert body["calls"]["success_rate"] == round(2 / 3, 3)
    assert body["top_agents"][0]["agent_id"] == "a1"
    assert body["top_agents"][0]["calls"] == 3


def test_digest_rejects_unknown_window(client):
    test_client, _ = client
    resp = test_client.get("/admin/usage/digest?window=banana", headers=AUTH_HEADERS)
    assert resp.status_code == 400
    assert "Unknown window" in resp.text


# ── Inspect ────────────────────────────────────────────────────────────────


def test_inspect_rejects_unknown_entity(client):
    test_client, _ = client
    resp = test_client.get(
        "/admin/usage/inspect?entity=goose&id=whatever",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "Unknown entity" in resp.text


def test_inspect_decision_returns_seeded_row(client):
    test_client, db_path = client
    decision_id = uuid.uuid4().hex
    _seed_decisions(db_path, [{
        "decision_id":  decision_id,
        "intent_text":  "format this YAML",
        "reason":       "no_match",
        "auto_invoked": 0,
        "candidates":   [{"agent_id": "a1", "name": "cand"}],
        "created_at":   _now_iso(),
    }])
    resp = test_client.get(
        f"/admin/usage/inspect?entity=decision&id={decision_id}",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["intent_text"] == "format this YAML"
    assert data["reason"] == "no_match"
    assert data["candidates"][0]["agent_id"] == "a1"
    # candidates_json must have been unpacked into candidates
    assert "candidates_json" not in data


def test_inspect_missing_entity_returns_404(client):
    test_client, _ = client
    resp = test_client.get(
        "/admin/usage/inspect?entity=job&id=does-not-exist",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404


# ── Query views ────────────────────────────────────────────────────────────


def test_query_rejects_unknown_view(client):
    test_client, _ = client
    resp = test_client.get(
        "/admin/usage/query?view=galaxy&window=24h",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "Unknown view" in resp.text


def test_query_no_match_groups_by_intent_hash(client):
    test_client, db_path = client
    now = _now_iso()
    _seed_decisions(db_path, [
        {"intent_text": "format this YAML", "reason": "no_match", "created_at": now},
        {"intent_text": "format this YAML", "reason": "no_match", "created_at": now},
        {"intent_text": "lint my CSS",      "reason": "no_match", "created_at": now},
    ])
    resp = test_client.get(
        "/admin/usage/query?view=no_match&window=24h&limit=10",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    # Two distinct intents → two clusters; the duplicated one ranks first.
    assert len(rows) == 2
    assert rows[0]["hits"] == 2
    assert rows[0]["example_intent"] == "format this YAML"
    assert rows[1]["hits"] == 1


def test_query_failures_returns_error_codes(client):
    test_client, db_path = client
    now = _now_iso()
    _seed_mcp_log(db_path, [
        {"agent_id": "a1", "tool_name": "call_specialist",
         "invoked_at": now, "success": 1, "error_code": None},
        {"agent_id": "a1", "tool_name": "call_specialist",
         "invoked_at": now, "success": 0, "error_code": "agent.timeout"},
    ])
    resp = test_client.get(
        "/admin/usage/query?view=failures&window=24h",
        headers=AUTH_HEADERS,
    )
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["error_code"] == "agent.timeout"


def test_query_empty_views_return_sensible_empty(client):
    test_client, _ = client
    # All views must respond with 200 + rows list on a fresh DB rather than
    # 500. user_activity reports per-user activity so it may include the
    # system user that the startup path seeds — that's correct behaviour.
    user_facing_view_can_be_nonempty = {"user_activity"}
    for view in (
        "no_match", "failures", "agent_health", "user_activity",
        "top_agents", "dormant_users", "spend_by_user", "spend_by_agent",
        "latency_outliers", "recent_decisions",
    ):
        resp = test_client.get(
            f"/admin/usage/query?view={view}&window=7d",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, f"{view}: {resp.text}"
        body = resp.json()
        assert "rows" in body
        if view in user_facing_view_can_be_nonempty:
            assert isinstance(body["rows"], list)
        else:
            assert body["rows"] == [], f"{view}: expected empty rows, got {body['rows']!r}"


def test_query_top_agents_orders_by_call_count(client):
    test_client, db_path = client
    now = _now_iso()
    rows = [
        {"job_id": f"j{i}", "agent_id": "popular", "caller": "user:u",
         "status": "complete", "created_at": now} for i in range(5)
    ] + [
        {"job_id": f"q{i}", "agent_id": "rare", "caller": "user:u",
         "status": "complete", "created_at": now} for i in range(1)
    ]
    _seed_jobs(db_path, rows)
    resp = test_client.get(
        "/admin/usage/query?view=top_agents&window=24h",
        headers=AUTH_HEADERS,
    )
    out = resp.json()["rows"]
    assert out[0]["agent_id"] == "popular"
    assert out[0]["calls"] == 5
    assert out[1]["agent_id"] == "rare"
