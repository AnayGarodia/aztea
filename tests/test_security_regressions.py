import os
os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

import ipaddress
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from starlette.requests import Request

from core import auth
from core import disputes
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
    modules = (registry, payments, auth, jobs, disputes)

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

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = dict(headers or {})
        captured["timeout"] = timeout
        captured["allow_redirects"] = allow_redirects
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
    assert captured["allow_redirects"] is False


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
    assert forbidden.json()["message"] == "Not authorized to view this wallet."

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


def test_admin_ip_allowlist_blocks_and_allows_by_forwarded_for(client, monkeypatch):
    owner = _register_user()
    register = client.post(
        "/registry/register",
        headers=_auth_headers(owner["raw_api_key"]),
        json={
            "name": f"Allowlist Agent {uuid.uuid4().hex[:6]}",
            "description": "admin allowlist test",
            "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
            "price_per_call_usd": 0.05,
            "tags": ["allowlist-test"],
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
        },
    )
    assert register.status_code == 201, register.text
    agent_id = register.json()["agent_id"]

    monkeypatch.setattr(
        server,
        "_ADMIN_IP_ALLOWLIST_NETWORKS",
        [ipaddress.ip_network("198.51.100.0/24")],
    )

    blocked = client.post(
        f"/admin/agents/{agent_id}/suspend",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert blocked.status_code == 403

    allowed_headers = {
        **_auth_headers(TEST_MASTER_KEY),
        "X-Forwarded-For": "198.51.100.42",
    }
    allowed = client.post(
        f"/admin/agents/{agent_id}/suspend",
        headers=allowed_headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "suspended"


def test_wallet_deposit_blocks_cross_owner_topup(client):
    user_a = _register_user()
    user_b = _register_user()
    wallet_b = payments.get_or_create_wallet(f"user:{user_b['user_id']}")

    denied = client.post(
        "/wallets/deposit",
        headers=_auth_headers(user_a["raw_api_key"]),
        json={"wallet_id": wallet_b["wallet_id"], "amount_cents": 50, "memo": "unauthorized topup"},
    )
    assert denied.status_code == 403
    assert denied.json()["message"] == "Not authorized to deposit into this wallet."

    master_ok = client.post(
        "/wallets/deposit",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"wallet_id": wallet_b["wallet_id"], "amount_cents": 50, "memo": "admin topup"},
    )
    assert master_ok.status_code == 200, master_ok.text
    assert master_ok.json()["balance_cents"] == 50


def test_registry_register_blocks_private_endpoint_urls(client):
    user = _register_user()
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(user["raw_api_key"]),
        json={
            "name": f"Private endpoint {uuid.uuid4().hex[:6]}",
            "description": "should be blocked",
            "endpoint_url": "http://localhost:8000/analyze",
            "price_per_call_usd": 0.05,
            "tags": ["security"],
            "input_schema": {"type": "object"},
        },
    )
    assert resp.status_code == 400
    assert "localhost" in resp.json()["message"].lower()


def test_rate_limit_keying_groups_invalid_rotating_bearer_tokens(monkeypatch):
    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)
    monkeypatch.setattr(server._auth, "verify_api_key", lambda _: None)

    key1 = server._key_from_request(_make_request("Bearer invalid-key-1"))
    key2 = server._key_from_request(_make_request("Bearer invalid-key-2"))
    key3 = server._key_from_request(_make_request(None))

    assert key1 == "203.0.113.10"
    assert key2 == "203.0.113.10"
    assert key3 == "203.0.113.10"


def test_invalid_content_length_header_returns_400(client):
    resp = client.get(
        "/health",
        headers={
            "Authorization": f"Bearer {TEST_MASTER_KEY}",
            "Content-Length": "abc",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["message"] == "Invalid Content-Length header."


def test_auth_init_db_migrates_legacy_api_keys_schema(isolated_db):
    _close_module_conn(auth)
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            """
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO users (user_id, username, email, password_hash, salt, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-user-1",
                "legacy-user",
                "legacy-user@example.com",
                "deadbeef",
                "abcd",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            CREATE TABLE api_keys (
                key_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                api_key_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO api_keys (key_id, user_id, api_key_hash, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "legacy-key-1",
                "legacy-user-1",
                "legacyhash",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    auth.init_auth_db()

    with sqlite3.connect(isolated_db) as conn:
        conn.row_factory = sqlite3.Row
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
        assert {
            "key_id",
            "user_id",
            "key_hash",
            "key_prefix",
            "name",
            "scopes",
            "created_at",
            "last_used_at",
            "is_active",
        }.issubset(cols)

        migrated = conn.execute(
            "SELECT key_hash, key_prefix, name, scopes, is_active FROM api_keys WHERE key_id = ?",
            ("legacy-key-1",),
        ).fetchone()
        assert migrated is not None
        assert migrated["key_hash"]
        assert migrated["key_prefix"].startswith("am_")
        assert migrated["name"]
        assert migrated["scopes"]
        assert int(migrated["is_active"]) == 1

    suffix = uuid.uuid4().hex[:8]
    registered = auth.register_user(
        username=f"fresh-{suffix}",
        email=f"fresh-{suffix}@example.com",
        password="password123",
    )
    assert registered["raw_api_key"].startswith("am_")

    login = auth.login_user(f"fresh-{suffix}@example.com", "password123")
    assert login is not None
    assert login["raw_api_key"].startswith("am_")


def test_register_user_is_atomic_when_api_key_insert_fails(isolated_db):
    _close_module_conn(auth)
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            """
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE api_keys (
                key_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                api_key_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    with pytest.raises(sqlite3.DatabaseError):
        auth.register_user(
            username=f"atomic-{uuid.uuid4().hex[:6]}",
            email=f"atomic-{uuid.uuid4().hex[:8]}@example.com",
            password="password123",
        )

    with sqlite3.connect(isolated_db) as conn:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert user_count == 0


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


def test_registry_register_rejects_endpoint_url_credentials(client):
    user = _register_user()
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(user["raw_api_key"]),
        json={
            "name": f"Credential URL Agent {uuid.uuid4().hex[:6]}",
            "description": "should be blocked",
            "endpoint_url": "https://user:pass@example.com/agent",
            "price_per_call_usd": 0.05,
            "tags": ["security"],
            "input_schema": {"type": "object"},
        },
    )
    assert resp.status_code == 400
    assert "username or password" in resp.json()["message"].lower()


def test_registry_call_blocks_misconfigured_endpoint_without_charging(client, isolated_db, monkeypatch):
    user = _register_user()
    owner_id = f"user:{user['user_id']}"
    caller_wallet = payments.get_or_create_wallet(owner_id)
    payments.deposit(caller_wallet["wallet_id"], 500, "security test funds")

    agent_id = registry.register_agent(
        name=f"Misconfigured endpoint {uuid.uuid4().hex[:6]}",
        description="runtime endpoint validation",
        endpoint_url="https://agents.example.com/agent",
        price_per_call_usd=0.05,
        tags=["security-test"],
    )
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            "UPDATE agents SET endpoint_url = ? WHERE agent_id = ?",
            ("http://localhost:8000/private", agent_id),
        )

    called = {"count": 0}

    def fake_post(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("proxy should not be invoked for blocked endpoint")

    monkeypatch.setattr(server.http, "post", fake_post)

    before = payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"]
    resp = client.post(
        f"/registry/agents/{agent_id}/call",
        json={"ticker": "AAPL"},
        headers=_auth_headers(user["raw_api_key"]),
    )
    after = payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"]

    assert resp.status_code == 502
    assert "misconfigured" in resp.json()["message"].lower()
    assert called["count"] == 0
    assert after == before


def test_agent_internal_error_response_does_not_leak_details(client, monkeypatch):
    user = _register_user()
    leaked_text = "super-secret-token-123"
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")
    payments.deposit(wallet["wallet_id"], 500, "security test funds")

    def _boom(_body):
        raise RuntimeError(leaked_text)

    monkeypatch.setattr(server, "_invoke_code_review_agent", _boom)
    resp = client.post(
        f"/registry/agents/{server._CODEREVIEW_AGENT_ID}/call",
        headers=_auth_headers(user["raw_api_key"]),
        json={"code": "print('hello')", "language": "python", "focus": "all"},
    )

    assert resp.status_code == 500
    body = resp.json()
    assert body["message"] == "Agent execution failed."
    assert leaked_text not in body["message"]


def test_payments_refund_is_blocked_after_payout_settlement(isolated_db):
    payments.init_payments_db()
    caller = payments.get_or_create_wallet("user:caller")
    agent = payments.get_or_create_wallet("agent:test-agent")
    platform = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    payments.deposit(caller["wallet_id"], 1000, "fund test wallet")
    charge_tx_id = payments.pre_call_charge(caller["wallet_id"], 100, "test-agent")
    payments.post_call_payout(
        agent["wallet_id"],
        platform["wallet_id"],
        charge_tx_id,
        100,
        "test-agent",
    )

    before = payments.get_wallet(caller["wallet_id"])["balance_cents"]
    payments.post_call_refund(caller["wallet_id"], charge_tx_id, 100, "test-agent")
    after = payments.get_wallet(caller["wallet_id"])["balance_cents"]
    assert after == before

    with payments._conn() as conn:
        refunds = conn.execute(
            "SELECT COUNT(*) AS count FROM transactions WHERE related_tx_id = ? AND type = 'refund'",
            (charge_tx_id,),
        ).fetchone()["count"]
    assert int(refunds) == 0


def test_concurrent_pre_call_charge_cannot_overdraw_wallet(isolated_db):
    payments.init_payments_db()
    caller = payments.get_or_create_wallet("user:concurrency-caller")
    payments.deposit(caller["wallet_id"], 100, "concurrency seed funds")

    successful_charges: list[str] = []
    insufficient_count = 0
    unexpected_errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def _attempt_charge() -> None:
        nonlocal insufficient_count
        barrier.wait()
        try:
            tx_id = payments.pre_call_charge(caller["wallet_id"], 100, "agent:concurrency")
            with lock:
                successful_charges.append(tx_id)
        except payments.InsufficientBalanceError:
            with lock:
                insufficient_count += 1
        except Exception as exc:  # pragma: no cover - defensive guard for threaded path
            with lock:
                unexpected_errors.append(str(exc))

    t1 = threading.Thread(target=_attempt_charge, name="charge-race-1")
    t2 = threading.Thread(target=_attempt_charge, name="charge-race-2")
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(successful_charges) == 1
    assert insufficient_count == 1
    assert not unexpected_errors
    assert payments.get_wallet(caller["wallet_id"])["balance_cents"] == 0

    with payments._conn() as conn:
        charge_count = conn.execute(
            "SELECT COUNT(*) AS count FROM transactions WHERE wallet_id = ? AND type = 'charge'",
            (caller["wallet_id"],),
        ).fetchone()["count"]
    assert int(charge_count) == 1
