import os
os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

import sqlite3
import uuid
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from starlette.requests import Request

from core import auth
from core import jobs
from core import payments
from core import registry
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


def _make_request(auth_header: str | None, host: str = "203.0.113.10") -> Request:
    headers = []
    if auth_header is not None:
        headers.append((b"authorization", auth_header.encode("utf-8")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": headers,
        "client": (host, 12345),
        "server": ("testserver", 80),
        "state": {},
    }
    return Request(scope)


@pytest.fixture
def isolated_db(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-security-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs)

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


def test_proxy_call_does_not_forward_master_auth_to_external_endpoints(client, monkeypatch):
    user = _register_user()
    owner_id = f"user:{user['user_id']}"
    caller_wallet = payments.get_or_create_wallet(owner_id)
    payments.deposit(caller_wallet["wallet_id"], 500, "security test funds")

    endpoint_url = "https://external.example/agent"
    agent_id = registry.register_agent(
        name=f"External security test {uuid.uuid4().hex[:6]}",
        description="External endpoint for auth forwarding test",
        endpoint_url=endpoint_url,
        price_per_call_usd=0.05,
        tags=["security-test"],
    )

    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = dict(headers or {})
        captured["timeout"] = timeout
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"ok": true}'
        resp.headers["Content-Type"] = "application/json"
        return resp

    monkeypatch.setattr(server.http, "post", fake_post)

    resp = client.post(
        f"/registry/agents/{agent_id}/call",
        json={"ticker": "AAPL"},
        headers=_auth_headers(user["raw_api_key"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert captured["url"] == endpoint_url
    assert captured["headers"].get("Content-Type") == "application/json"
    assert "Authorization" not in captured["headers"]


def test_wallet_endpoint_authorization_allows_owner_and_master_only(client):
    user_a = _register_user()
    user_b = _register_user()
    wallet_a = payments.get_or_create_wallet(f"user:{user_a['user_id']}")
    wallet_b = payments.get_or_create_wallet(f"user:{user_b['user_id']}")

    forbidden = client.get(
        f"/wallets/{wallet_b['wallet_id']}",
        headers=_auth_headers(user_a["raw_api_key"]),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "Not authorized to view this wallet."

    owner_ok = client.get(
        f"/wallets/{wallet_a['wallet_id']}",
        headers=_auth_headers(user_a["raw_api_key"]),
    )
    assert owner_ok.status_code == 200
    assert owner_ok.json()["wallet_id"] == wallet_a["wallet_id"]

    master_ok = client.get(
        f"/wallets/{wallet_b['wallet_id']}",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert master_ok.status_code == 200
    assert master_ok.json()["wallet_id"] == wallet_b["wallet_id"]


def test_rate_limit_keying_groups_invalid_rotating_bearer_tokens(monkeypatch):
    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)
    monkeypatch.setattr(server._auth, "verify_api_key", lambda _: None)

    key1 = server._key_from_request(_make_request("Bearer invalid-key-1"))
    key2 = server._key_from_request(_make_request("Bearer invalid-key-2"))
    key3 = server._key_from_request(_make_request(None))

    assert key1 == "203.0.113.10"
    assert key2 == "203.0.113.10"
    assert key3 == "203.0.113.10"


def test_registry_init_db_migrates_legacy_agents_table(isolated_db):
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("""
            CREATE TABLE agents (
                name TEXT,
                endpoint_url TEXT,
                price_per_call_usd REAL
            )
        """)
        conn.execute(
            """
            INSERT INTO agents (name, endpoint_url, price_per_call_usd)
            VALUES (?, ?, ?)
            """,
            ("Legacy Agent", None, -7.5),
        )

    registry.init_db()

    with sqlite3.connect(isolated_db) as conn:
        conn.row_factory = sqlite3.Row
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
        required = {
            "agent_id",
            "owner_id",
            "name",
            "description",
            "endpoint_url",
            "price_per_call_usd",
            "avg_latency_ms",
            "total_calls",
            "successful_calls",
            "tags",
            "input_schema",
            "created_at",
        }
        assert required.issubset(cols)

        row = conn.execute("SELECT * FROM agents").fetchone()
        assert row is not None
        assert row["name"] == "Legacy Agent"
        assert row["description"] == "No description provided."
        assert row["endpoint_url"].startswith("legacy://missing-endpoint/")
        assert row["price_per_call_usd"] == 0.0
        assert row["created_at"] == "1970-01-01T00:00:00+00:00"

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO agents
                  (agent_id, owner_id, name, description, endpoint_url, price_per_call_usd,
                   avg_latency_ms, total_calls, successful_calls, tags, input_schema, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    "user:test-owner",
                    "Bad Price Agent",
                    "Should fail check",
                    "https://example.com/bad",
                    -0.01,
                    0.0,
                    0,
                    0,
                    "[]",
                    "{}",
                    "2026-01-01T00:00:00+00:00",
                ),
            )


@pytest.mark.parametrize("bad_price", [-0.01, float("inf"), float("nan"), "bad"])
def test_register_agent_rejects_invalid_price_values(isolated_db, bad_price):
    registry.init_db()
    with pytest.raises(ValueError):
        registry.register_agent(
            name=f"Invalid price {uuid.uuid4().hex[:6]}",
            description="validation check",
            endpoint_url="https://example.com/agent",
            price_per_call_usd=bad_price,
            tags=[],
        )
