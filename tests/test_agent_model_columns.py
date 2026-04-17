"""Tests for model_provider / model_id fields on agent listings."""
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import auth
from core import disputes
from core import jobs
from core import payments
from core import registry
from core import reputation
import server

TEST_MASTER_KEY = "test-master-key-model-cols"


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


@pytest.fixture
def fake_embeddings(monkeypatch):
    import numpy as np

    def embed_text(text):
        return [float(hash(text) % 1000) / 1000.0] * 384

    def cosine(a, b):
        a = np.asarray(a, dtype=np.float32)
        b = np.asarray(b, dtype=np.float32)
        norm_a = float(np.linalg.norm(a))
        norm_b = float(np.linalg.norm(b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    monkeypatch.setattr(registry.embeddings, "embed_text", embed_text)
    monkeypatch.setattr(registry.embeddings, "cosine", cosine)


@pytest.fixture
def isolated_db(monkeypatch, fake_embeddings):
    db_path = Path(__file__).resolve().parent / f"test-model-cols-{uuid.uuid4().hex}.db"
    modules = (registry, reputation, payments, auth, jobs, disputes)

    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))

    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)
    yield db_path

    for module in modules:
        _close_module_conn(module)

    for suffix in ("", "-shm", "-wal"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------------------
# Pure registry layer tests (no HTTP)
# ---------------------------------------------------------------------------

def test_register_agent_persists_model_columns(isolated_db):
    registry.init_db()
    aid = registry.register_agent(
        name="OpenAI Test Agent",
        description="Uses GPT-4o-mini",
        endpoint_url="https://example.com/agent",
        price_per_call_usd=0.01,
        tags=["test"],
        model_provider="openai",
        model_id="gpt-4o-mini",
        embed_listing=False,
    )
    agent = registry.get_agent(aid)
    assert agent["model_provider"] == "openai"
    assert agent["model_id"] == "gpt-4o-mini"


def test_register_agent_model_columns_nullable(isolated_db):
    registry.init_db()
    aid = registry.register_agent(
        name="No Model Agent",
        description="Does not use an LLM",
        endpoint_url="https://example.com/nomllm",
        price_per_call_usd=0.005,
        tags=["test"],
        embed_listing=False,
    )
    agent = registry.get_agent(aid)
    assert agent["model_provider"] is None
    assert agent["model_id"] is None


def test_get_agents_filters_by_provider(isolated_db):
    registry.init_db()
    registry.register_agent(
        name="Groq Agent",
        description="Uses Groq",
        endpoint_url="https://example.com/groq",
        price_per_call_usd=0.01,
        tags=[],
        model_provider="groq",
        model_id="llama-3.3-70b-versatile",
        embed_listing=False,
    )
    registry.register_agent(
        name="OpenAI Agent",
        description="Uses OpenAI",
        endpoint_url="https://example.com/openai",
        price_per_call_usd=0.02,
        tags=[],
        model_provider="openai",
        model_id="gpt-4o-mini",
        embed_listing=False,
    )
    openai_agents = registry.get_agents(model_provider="openai")
    assert len(openai_agents) == 1
    assert openai_agents[0]["name"] == "OpenAI Agent"

    groq_agents = registry.get_agents(model_provider="groq")
    assert len(groq_agents) == 1
    assert groq_agents[0]["name"] == "Groq Agent"


def test_get_agents_no_filter_returns_all(isolated_db):
    registry.init_db()
    registry.register_agent(
        name="Alpha Agent",
        description="first",
        endpoint_url="https://example.com/a",
        price_per_call_usd=0.01,
        tags=[],
        model_provider="groq",
        embed_listing=False,
    )
    registry.register_agent(
        name="Beta Agent",
        description="second",
        endpoint_url="https://example.com/b",
        price_per_call_usd=0.01,
        tags=[],
        model_provider="anthropic",
        embed_listing=False,
    )
    all_agents = registry.get_agents()
    assert len(all_agents) == 2


def test_get_agents_invalid_provider_raises(isolated_db):
    registry.init_db()
    with pytest.raises(ValueError, match="model_provider"):
        registry.get_agents(model_provider="martian")


# ---------------------------------------------------------------------------
# HTTP layer tests
# ---------------------------------------------------------------------------

def test_builtin_agents_registered_with_groq_provider(isolated_db):
    # Builtins are internal-only; we check directly via registry layer after startup
    with TestClient(server.app):
        pass  # triggers lifespan → ensure_builtin_agents_registered()
    all_agents = registry.get_agents(include_internal=True)
    builtins = [a for a in all_agents if a.get("internal_only")]
    assert len(builtins) > 0, "No built-in agents found after startup"
    for agent in builtins:
        assert agent.get("model_provider") == "groq", (
            f"Built-in '{agent['name']}' has model_provider={agent.get('model_provider')!r}"
        )


def test_api_register_agent_with_model_fields(isolated_db):
    with TestClient(server.app) as client:
        reg = client.post(
            "/auth/register",
            json={"username": "modeltest", "email": "modeltest@test.com", "password": "pass123!"},
        )
        assert reg.status_code == 201
        api_key = reg.json()["raw_api_key"]

        resp = client.post(
            "/registry/register",
            headers=_auth_headers(api_key),
            json={
                "name": "My Anthropic Agent",
                "description": "Uses Claude",
                "endpoint_url": "https://example.com/claude-agent",
                "price_per_call_usd": 0.03,
                "tags": ["ai"],
                "model_provider": "anthropic",
                "model_id": "claude-sonnet-4-6",
            },
        )
        assert resp.status_code == 201, resp.text
        agent = resp.json()["agent"]
        assert agent["model_provider"] == "anthropic"
        assert agent["model_id"] == "claude-sonnet-4-6"


def test_api_filter_agents_by_provider(isolated_db):
    with TestClient(server.app) as client:
        resp = client.get(
            "/registry/agents?model_provider=groq",
            headers=_auth_headers(TEST_MASTER_KEY),
        )
        assert resp.status_code == 200
        agents = resp.json()["agents"]
        for a in agents:
            assert a["model_provider"] == "groq"


def test_api_filter_agents_invalid_provider(isolated_db):
    with TestClient(server.app) as client:
        resp = client.get(
            "/registry/agents?model_provider=martian",
            headers=_auth_headers(TEST_MASTER_KEY),
        )
        assert resp.status_code == 400
