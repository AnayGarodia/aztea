import sqlite3
import uuid
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core import auth
from core import disputes
from core import jobs
from core import payments
from core import registry
from core import reputation
import server.application as server

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


def _set_agent_stats(
    db_path: Path,
    agent_id: str,
    total_calls: int,
    successful_calls: int,
    avg_latency_ms: float,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE agents
            SET total_calls = ?, successful_calls = ?, avg_latency_ms = ?
            WHERE agent_id = ?
            """,
            (total_calls, successful_calls, avg_latency_ms, agent_id),
        )


@pytest.fixture
def fake_embeddings(monkeypatch):
    vocab = [
        "sec",
        "filing",
        "10-k",
        "10-q",
        "quarterly",
        "report",
        "summarize",
        "financial",
        "ticker",
        "python",
        "code",
        "bug",
        "review",
        "image",
        "generator",
    ]
    dim = registry.embeddings.EMBEDDING_DIM

    def embed_text(text: str) -> list[float]:
        lowered = str(text or "").lower()
        vec = np.zeros(dim, dtype=np.float32)

        for idx, term in enumerate(vocab):
            if term in lowered:
                vec[idx] += 1.0

        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec.tolist()

    def cosine(a, b) -> float:
        arr_a = np.asarray(a, dtype=np.float32).reshape(-1)
        arr_b = np.asarray(b, dtype=np.float32).reshape(-1)
        denom = float(np.linalg.norm(arr_a) * np.linalg.norm(arr_b))
        if denom == 0.0:
            return 0.0
        return float(np.dot(arr_a, arr_b) / denom)

    monkeypatch.setattr(registry.embeddings, "embed_text", embed_text)
    monkeypatch.setattr(registry.embeddings, "cosine", cosine)


@pytest.fixture
def isolated_db(monkeypatch, fake_embeddings):
    db_path = Path(__file__).resolve().parent / f"test-registry-{uuid.uuid4().hex}.db"
    modules = (registry, reputation, payments, auth, jobs, disputes)

    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))

    # Apply real migrations so every test starts with the full schema
    # regardless of pytest-randomly ordering. Without this, a test that
    # ran before any init_db() caller would see `no such table: agents`.
    from core.migrate import apply_migrations as _apply_migrations
    _apply_migrations(str(db_path))

    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)
    yield db_path

    for module in modules:
        _close_module_conn(module)

    for suffix in ("", "-shm", "-wal"):
        path = Path(f"{db_path}{suffix}")
        if path.exists():
            path.unlink()


def _seed_synthetic_agents(db_path: Path) -> dict[str, str]:
    registry.init_db()
    reputation.init_reputation_db()

    filing = registry.register_agent(
        name="SEC Filing Analyzer",
        description="Summarizes quarterly and annual SEC filings into concise investment briefs.",
        endpoint_url="https://agents.example.com/filings",
        price_per_call_usd=0.06,
        tags=["finance", "sec", "filings"],
        input_schema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "filing_type": {"type": "string"},
            },
        },
    )
    image = registry.register_agent(
        name="Image Generator",
        description="Generates photorealistic images from text prompts.",
        endpoint_url="https://agents.example.com/images",
        price_per_call_usd=0.03,
        tags=["image", "creative"],
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
            },
        },
    )
    code = registry.register_agent(
        name="Python Code Reviewer",
        description="Finds bugs and reliability issues in Python services.",
        endpoint_url="https://agents.example.com/code-review",
        price_per_call_usd=0.12,
        tags=["code-review", "python", "bugs"],
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "language": {"type": "string"},
            },
        },
    )

    _set_agent_stats(
        db_path, filing, total_calls=50, successful_calls=47, avg_latency_ms=450
    )
    _set_agent_stats(
        db_path, image, total_calls=6, successful_calls=3, avg_latency_ms=1900
    )
    _set_agent_stats(
        db_path, code, total_calls=35, successful_calls=31, avg_latency_ms=550
    )
    return {"filing": filing, "image": image, "code": code}


def test_register_agent_auto_embeds_same_request(isolated_db):
    registry.init_db()
    agent_id = registry.register_agent(
        name="Auto Embed Agent",
        description="Checks same-request embedding write on registration.",
        endpoint_url="https://agents.example.com/auto-embed",
        price_per_call_usd=0.01,
        tags=["embed-test"],
        input_schema={"type": "object", "properties": {"ticker": {"type": "string"}}},
    )

    with sqlite3.connect(isolated_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT source_text, embedding FROM agent_embeddings WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()

    assert row is not None
    assert "Auto Embed Agent" in row["source_text"]
    vector = np.frombuffer(row["embedding"], dtype=np.float32)
    assert vector.size == registry.embeddings.EMBEDDING_DIM


def test_semantic_search_ranks_relevant_agents_first(isolated_db):
    ids = _seed_synthetic_agents(isolated_db)

    filings = registry.search_agents("analyze a quarterly SEC report", limit=3)
    assert filings[0]["agent"]["agent_id"] == ids["filing"]

    code = registry.search_agents("find bugs in python code", limit=3)
    assert code[0]["agent"]["agent_id"] == ids["code"]


def test_semantic_search_applies_filters_and_weighting(isolated_db, monkeypatch):
    # 3-agent fixture has only one strong match for "find bugs in python
    # code" (the Python Code Reviewer); the other two are deliberate
    # weak peers (SEC filings, image gen) included to validate that the
    # ranker SURFACES all three with their blended scores in the right
    # order. On the production floors the weak peers correctly fall
    # under the post-rank dropoff filter. Lower BOTH floors for this
    # test so we can still inspect the ranking math against all three.
    monkeypatch.setenv("AZTEA_SEARCH_RELEVANCE_FLOOR", "0.0")
    monkeypatch.setenv("AZTEA_SEARCH_KEEP_FLOOR", "0.0")
    ids = _seed_synthetic_agents(isolated_db)
    results = registry.search_agents("find bugs in python code", limit=3)
    assert len(results) == 3

    prices = [
        registry._price_usd_to_cents(item["agent"]["price_per_call_usd"])
        for item in results
    ]
    min_price = min(prices)
    max_price = max(prices)

    for item, price_cents in zip(results, prices):
        if max_price == min_price:
            inverse_price = 1.0
        else:
            inverse_price = 1.0 - ((price_cents - min_price) / (max_price - min_price))
        intent_bonus = registry._intent_match_bonus(
            "find bugs in python code", item["agent"]
        )
        expected = (
            registry.LEXICAL_SCORE_WEIGHT * item["lexical_score"]
            + registry.SEMANTIC_SCORE_WEIGHT * item["similarity"]
            + registry.TRUST_SCORE_WEIGHT_HYBRID * item["trust"]
            + registry.INVERSE_PRICE_WEIGHT_HYBRID * inverse_price
            + intent_bonus
        )
        assert item["blended_score"] == pytest.approx(round(expected, 6), abs=1e-5)

    budget = registry.search_agents(
        "find bugs in python code", limit=3, max_price_cents=8
    )
    assert all(
        registry._price_usd_to_cents(item["agent"]["price_per_call_usd"]) <= 8
        for item in budget
    )
    assert all(item["agent"]["agent_id"] != ids["code"] for item in budget)

    ticker_only = registry.search_agents(
        "summarize a 10-k filing",
        limit=3,
        required_input_fields=["ticker"],
    )
    assert ticker_only[0]["agent"]["agent_id"] == ids["filing"]
    for item in ticker_only:
        props = item["agent"]["input_schema"].get("properties", {})
        assert "ticker" in props

    strict_trust = registry.search_agents(
        "analyze a quarterly SEC report", limit=3, min_trust=0.7
    )
    assert all(item["trust"] >= 0.7 for item in strict_trust)


def test_search_falls_back_to_lexical_scoring_when_embeddings_disabled(
    isolated_db, monkeypatch
):
    ids = _seed_synthetic_agents(isolated_db)
    monkeypatch.setattr(registry._feature_flags, "DISABLE_EMBEDDINGS", True)

    results = registry.search_agents("quarterly sec filing ticker", limit=3)

    assert results[0]["agent"]["agent_id"] == ids["filing"]
    assert results[0]["similarity"] == 0.0
    assert results[0]["lexical_score"] > 0.5


def test_search_uses_output_examples_as_lexical_signal(isolated_db, monkeypatch):
    # Single-agent fixture: blended_score lands ~0.22 because the catalog
    # has no peers to normalise against. Lower the relevance floor for
    # this test so we still verify the ranking signal we care about
    # (output_examples lifting an agent into the top spot).
    monkeypatch.setenv("AZTEA_SEARCH_RELEVANCE_FLOOR", "0.10")
    registry.init_db()
    reputation.init_reputation_db()
    agent_id = registry.register_agent(
        name="Testing Workflow Advisor",
        description="Helps design and review testing workflows.",
        endpoint_url="https://agents.example.com/testing-workflow",
        price_per_call_usd=0.04,
        tags=["testing", "qa"],
        input_schema={"type": "object", "properties": {"task": {"type": "string"}}},
        output_examples=[
            {
                "input": {"task": "generate pytest coverage plan"},
                "output": {
                    "summary": "Create a pytest suite and coverage thresholds for the module."
                },
            }
        ],
    )
    _set_agent_stats(
        isolated_db, agent_id, total_calls=8, successful_calls=8, avg_latency_ms=300
    )

    results = registry.search_agents("pytest coverage plan", limit=5)
    top = results[0]
    assert top["agent"]["agent_id"] == agent_id
    # The lexical scorer was retuned in 738043c; for this fixture it now lands
    # around 0.22. The intent of the test is "output_examples contribute to
    # ranking" (proven by the agent ranking #1 on a query that only matches
    # its example, not its description) — keep the threshold low enough to
    # be stable across small algorithm tweaks.
    assert top["lexical_score"] > 0.15
    assert any("work examples" in reason for reason in top["match_reasons"])



def test_security_queries_prefer_dependency_audit_over_package_finder(isolated_db):
    registry.init_db()
    reputation.init_reputation_db()

    dependency_auditor = registry.register_agent(
        name="Dependency Auditor",
        description="Find vulnerabilities, CVEs, and outdated packages in npm and Python manifests.",
        endpoint_url="https://agents.example.com/dependency-auditor",
        price_per_call_usd=0.04,
        tags=["dependency-audit", "security", "npm", "cve"],
        input_schema={"type": "object", "properties": {"manifest": {"type": "string"}}},
    )
    package_finder = registry.register_agent(
        name="Package Finder",
        description="Suggest npm and PyPI packages based on a natural-language description.",
        endpoint_url="https://agents.example.com/package-finder",
        price_per_call_usd=0.02,
        tags=["packages", "discovery"],
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )

    _set_agent_stats(
        isolated_db,
        dependency_auditor,
        total_calls=8,
        successful_calls=8,
        avg_latency_ms=600,
    )
    _set_agent_stats(
        isolated_db,
        package_finder,
        total_calls=30,
        successful_calls=30,
        avg_latency_ms=300,
    )

    results = registry.search_agents(
        "find security vulnerabilities in npm package", limit=5
    )
    assert results[0]["agent"]["agent_id"] == dependency_auditor
    assert all(
        item["agent"]["agent_id"] != package_finder or idx > 0
        for idx, item in enumerate(results)
    )


def test_price_queries_rank_by_price(isolated_db):
    registry.init_db()
    reputation.init_reputation_db()

    cheap = registry.register_agent(
        name="Cheap Runtime",
        description="Runs simple sandbox tasks.",
        endpoint_url="https://agents.example.com/cheap",
        price_per_call_usd=0.01,
        tags=["runtime"],
        input_schema={"type": "object", "properties": {"task": {"type": "string"}}},
    )
    expensive = registry.register_agent(
        name="Expensive Runtime",
        description="Runs simple sandbox tasks.",
        endpoint_url="https://agents.example.com/expensive",
        price_per_call_usd=0.20,
        tags=["runtime"],
        input_schema={"type": "object", "properties": {"task": {"type": "string"}}},
    )
    _set_agent_stats(
        isolated_db, cheap, total_calls=1, successful_calls=1, avg_latency_ms=10.0
    )
    _set_agent_stats(
        isolated_db,
        expensive,
        total_calls=100,
        successful_calls=100,
        avg_latency_ms=10.0,
    )

    cheapest = registry.search_agents("the cheapest agent", limit=2)
    assert cheapest[0]["agent"]["agent_id"] == cheap

    costliest = registry.search_agents("the most expensive agent", limit=2)
    assert costliest[0]["agent"]["agent_id"] == expensive


def test_semantic_outranks_spurious_lexical_overlap(isolated_db):
    """Regression for the 2026-05-07 power-user eval. Lexical overlap on a
    shared token (``base64`` for JWT/image, ``screenshot`` for browser/diff)
    used to outrank intent matches because LEXICAL_SCORE_WEIGHT was higher
    than SEMANTIC_SCORE_WEIGHT. Prove the new 0.30/0.50 split routes
    intent-matching queries to the right agent."""
    registry.init_db()
    reputation.init_reputation_db()

    browser = registry.register_agent(
        name="Browser Agent",
        description="Use when you need to fetch a live web page with a real browser. Launches headless Chromium and supports screenshot capture.",
        endpoint_url="https://agents.example.com/browser",
        price_per_call_usd=0.03,
        tags=["browser", "screenshot", "playwright"],
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
    )
    visual_diff = registry.register_agent(
        name="Visual Regression",
        description="Compares two screenshot artifacts and computes a pixel-level diff. Useful for visual regression testing of rendered pages.",
        endpoint_url="https://agents.example.com/visual",
        price_per_call_usd=0.03,
        tags=["visual", "diff", "screenshot"],
        input_schema={
            "type": "object",
            "properties": {"left": {"type": "string"}, "right": {"type": "string"}},
        },
    )
    _set_agent_stats(
        isolated_db, browser, total_calls=10, successful_calls=10, avg_latency_ms=10.0
    )
    _set_agent_stats(
        isolated_db,
        visual_diff,
        total_calls=10,
        successful_calls=10,
        avg_latency_ms=10.0,
    )

    # The eval saw "screenshot a website" rank visual_regression #1 over
    # browser_agent. With semantic > lexical and the screenshot/website
    # query expansions removed, the right agent should win.
    results = registry.search_agents("screenshot a website", limit=2)
    assert results[0]["agent"]["agent_id"] == browser, [
        r["agent"]["name"] for r in results
    ]
