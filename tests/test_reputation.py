import sqlite3
import uuid
from pathlib import Path

import pytest

from core import jobs
from core import registry
from core import reputation
from core import disputes


def _close_module_conn(module) -> None:
    conn = getattr(module._local, "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(module._local, "conn")
    except AttributeError:
        pass


@pytest.fixture
def isolated_db(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-reputation-{uuid.uuid4().hex}.db"
    modules = (registry, jobs, reputation, disputes)

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


def _register_agent(name_suffix: str) -> str:
    return registry.register_agent(
        name=f"Reputation Agent {name_suffix}",
        description="Reputation test agent",
        endpoint_url=f"https://example.com/{name_suffix}",
        price_per_call_usd=0.05,
        tags=["reputation-test"],
    )


def _create_job(agent_id: str, caller_owner_id: str) -> dict:
    return jobs.create_job(
        agent_id=agent_id,
        caller_owner_id=caller_owner_id,
        caller_wallet_id=str(uuid.uuid4()),
        agent_wallet_id=str(uuid.uuid4()),
        platform_wallet_id=str(uuid.uuid4()),
        price_cents=25,
        charge_tx_id=str(uuid.uuid4()),
        input_payload={"task": "test"},
    )


def _set_agent_stats(db_path: Path, agent_id: str, total_calls: int, successful_calls: int, avg_latency_ms: float) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE agents
            SET total_calls = ?, successful_calls = ?, avg_latency_ms = ?
            WHERE agent_id = ?
            """,
            (total_calls, successful_calls, avg_latency_ms, agent_id),
        )


def _insert_quality_ratings(db_path: Path, agent_id: str, ratings: list[int]) -> None:
    with sqlite3.connect(db_path) as conn:
        for rating in ratings:
            conn.execute(
                """
                INSERT INTO job_quality_ratings (job_id, agent_id, caller_owner_id, rating, created_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                """,
                (str(uuid.uuid4()), agent_id, f"user:{uuid.uuid4().hex[:8]}", rating),
            )


@pytest.mark.parametrize("bad_rating", [0, 6, -1, 2.5, "5", True])
def test_record_job_quality_rating_rejects_invalid_values(isolated_db, bad_rating):
    registry.init_db()
    jobs.init_jobs_db()
    reputation.init_reputation_db()

    caller_owner_id = f"user:{uuid.uuid4().hex[:8]}"
    agent_id = _register_agent(uuid.uuid4().hex[:6])
    job = _create_job(agent_id, caller_owner_id)
    jobs.update_job_status(job["job_id"], "complete", output_payload={"ok": True}, completed=True)

    with pytest.raises(ValueError):
        reputation.record_job_quality_rating(job["job_id"], caller_owner_id, bad_rating)


def test_record_job_quality_rating_requires_completed_job_and_allows_revision(isolated_db):
    """Bug 2 (2026-05-18): a caller may revise their own rating on the same job.

    The pre-1.7.20 behaviour raised ``ValueError("...already has a quality
    rating")`` on the second call. Now the helper UPSERTs and surfaces
    ``previous_rating`` + ``revised`` so callers can tell an edit from a fresh
    rating. A DIFFERENT caller's rating on the same job is still rejected.
    """
    registry.init_db()
    jobs.init_jobs_db()
    reputation.init_reputation_db()

    caller_owner_id = f"user:{uuid.uuid4().hex[:8]}"
    agent_id = _register_agent(uuid.uuid4().hex[:6])
    job = _create_job(agent_id, caller_owner_id)

    with pytest.raises(ValueError, match="completed"):
        reputation.record_job_quality_rating(job["job_id"], caller_owner_id, 5)

    jobs.update_job_status(job["job_id"], "complete", output_payload={"ok": True}, completed=True)

    with pytest.raises(ValueError, match="caller"):
        reputation.record_job_quality_rating(job["job_id"], "user:someone-else", 5)

    created = reputation.record_job_quality_rating(job["job_id"], caller_owner_id, 5)
    assert created["job_id"] == job["job_id"]
    assert created["agent_id"] == agent_id
    assert created["rating"] == 5
    assert created["revised"] is False
    assert created["previous_rating"] is None

    revised = reputation.record_job_quality_rating(job["job_id"], caller_owner_id, 4)
    assert revised["rating"] == 4
    assert revised["revised"] is True
    assert revised["previous_rating"] == 5

    # A different caller still hits the eligibility gate (Only the job
    # caller can rate) BEFORE reaching the upsert.
    with pytest.raises(ValueError, match="caller"):
        reputation.record_job_quality_rating(job["job_id"], "user:other-rater", 1)


def test_trust_score_math_tracks_quality_success_latency_and_volume(isolated_db):
    registry.init_db()
    reputation.init_reputation_db()

    quality_high = _register_agent("quality-high")
    quality_low = _register_agent("quality-low")
    success_high = _register_agent("success-high")
    success_low = _register_agent("success-low")
    latency_fast = _register_agent("latency-fast")
    latency_slow = _register_agent("latency-slow")
    volume_high = _register_agent("volume-high")
    volume_low = _register_agent("volume-low")

    _set_agent_stats(isolated_db, quality_high, total_calls=20, successful_calls=18, avg_latency_ms=1200)
    _set_agent_stats(isolated_db, quality_low, total_calls=20, successful_calls=18, avg_latency_ms=1200)
    _insert_quality_ratings(isolated_db, quality_high, [5, 5, 5, 5])
    _insert_quality_ratings(isolated_db, quality_low, [2, 2, 2, 2])

    _set_agent_stats(isolated_db, success_high, total_calls=20, successful_calls=19, avg_latency_ms=1200)
    _set_agent_stats(isolated_db, success_low, total_calls=20, successful_calls=8, avg_latency_ms=1200)
    _insert_quality_ratings(isolated_db, success_high, [4, 4, 4, 4])
    _insert_quality_ratings(isolated_db, success_low, [4, 4, 4, 4])

    _set_agent_stats(isolated_db, latency_fast, total_calls=20, successful_calls=18, avg_latency_ms=250)
    _set_agent_stats(isolated_db, latency_slow, total_calls=20, successful_calls=18, avg_latency_ms=6000)
    _insert_quality_ratings(isolated_db, latency_fast, [4, 4, 4, 4])
    _insert_quality_ratings(isolated_db, latency_slow, [4, 4, 4, 4])

    _set_agent_stats(isolated_db, volume_high, total_calls=60, successful_calls=54, avg_latency_ms=800)
    _set_agent_stats(isolated_db, volume_low, total_calls=2, successful_calls=2, avg_latency_ms=800)
    _insert_quality_ratings(isolated_db, volume_high, [5] * 20)
    _insert_quality_ratings(isolated_db, volume_low, [5])

    enriched = reputation.enrich_agent_records(registry.get_agents())
    by_id = {item["agent_id"]: item for item in enriched}

    assert by_id[quality_high]["reputation"]["quality_score"] > by_id[quality_low]["reputation"]["quality_score"]
    assert by_id[quality_high]["trust_score"] > by_id[quality_low]["trust_score"]

    assert by_id[success_high]["reputation"]["success_score"] > by_id[success_low]["reputation"]["success_score"]
    assert by_id[success_high]["trust_score"] > by_id[success_low]["trust_score"]

    assert by_id[latency_fast]["reputation"]["latency_score"] > by_id[latency_slow]["reputation"]["latency_score"]
    assert by_id[latency_fast]["trust_score"] > by_id[latency_slow]["trust_score"]

    assert by_id[volume_high]["reputation"]["confidence_score"] > by_id[volume_low]["reputation"]["confidence_score"]
    assert by_id[volume_high]["trust_score"] > by_id[volume_low]["trust_score"]


class _FakeHostedClient:
    """Stand-in for HostedClient used in blend tests; never touches the network."""

    def __init__(self, *, enabled: bool, response: dict | None) -> None:
        self._enabled = enabled
        self._response = response
        self.fetch_calls: list[str] = []

    def is_enabled(self) -> bool:
        return self._enabled

    def fetch_trust(self, did: str) -> dict | None:
        self.fetch_calls.append(did)
        return self._response


def _install_fake_hosted_client(
    monkeypatch, *, hosted_url: str | None, client: _FakeHostedClient | None,
) -> None:
    """Wire up env + monkeypatched get_hosted_client for blend tests."""
    if hosted_url is None:
        monkeypatch.delenv("AZTEA_HOSTED_API_URL", raising=False)
    else:
        monkeypatch.setenv("AZTEA_HOSTED_API_URL", hosted_url)
    if client is not None:
        import core.hosted_client as hosted_client_module

        monkeypatch.setattr(
            hosted_client_module, "get_hosted_client", lambda: client
        )


def test_compute_trust_metrics_no_blend_when_hosted_disabled(isolated_db, monkeypatch):
    registry.init_db()
    reputation.init_reputation_db()
    agent_id = _register_agent("oss-mode")
    _set_agent_stats(isolated_db, agent_id, total_calls=4, successful_calls=4, avg_latency_ms=500)
    _insert_quality_ratings(isolated_db, agent_id, [5, 5])

    _install_fake_hosted_client(monkeypatch, hosted_url=None, client=None)
    metrics = reputation.compute_trust_metrics(agent_id)

    assert metrics["blended_global_weight"] == 0.0
    # Recomputing without the blend (hosted disabled) must yield the same number.
    bare = reputation._build_trust_metrics(
        agent_id=agent_id,
        total_calls=4, successful_calls=4, avg_latency_ms=500,
        rating_count=2, average_quality_rating=5.0,
        decay_multiplier=1.0,
    )
    assert metrics["trust_score"] == bare["trust_score"]


def test_compute_trust_metrics_blends_when_hosted_enabled_and_low_evidence(
    isolated_db, monkeypatch,
):
    registry.init_db()
    reputation.init_reputation_db()
    agent_id = _register_agent("blend-low-ev")
    # 1 call + 0 ratings → evidence count 1, local_weight = 1/20 = 0.05.
    _set_agent_stats(isolated_db, agent_id, total_calls=1, successful_calls=1, avg_latency_ms=500)
    # Give the agent a DID so the hosted lookup actually runs.
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("UPDATE agents SET did = ? WHERE agent_id = ?", ("did:web:test:agents:x", agent_id))

    fake = _FakeHostedClient(enabled=True, response={"trust_score": 90.0})
    _install_fake_hosted_client(monkeypatch, hosted_url="https://hosted.test", client=fake)

    local_only = reputation._build_trust_metrics(
        agent_id=agent_id,
        total_calls=1, successful_calls=1, avg_latency_ms=500,
        rating_count=0, average_quality_rating=None,
        decay_multiplier=1.0,
    )["trust_score"]

    metrics = reputation.compute_trust_metrics(agent_id)
    assert fake.fetch_calls == ["did:web:test:agents:x"]
    # global_weight = 1 - 1/20 = 0.95, so the blended score lives close to 90.
    assert metrics["blended_global_weight"] == pytest.approx(0.95, abs=1e-4)
    expected = round(local_only * 0.05 + 90.0 * 0.95, 2)
    assert metrics["trust_score"] == pytest.approx(expected, abs=0.01)


def test_compute_trust_metrics_local_dominates_at_threshold(isolated_db, monkeypatch):
    registry.init_db()
    reputation.init_reputation_db()
    agent_id = _register_agent("blend-high-ev")
    # 20 calls → evidence count 20, local_weight = 1.0, blended == local.
    _set_agent_stats(isolated_db, agent_id, total_calls=20, successful_calls=18, avg_latency_ms=800)
    _insert_quality_ratings(isolated_db, agent_id, [4, 4, 4])
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("UPDATE agents SET did = ? WHERE agent_id = ?", ("did:web:test:agents:y", agent_id))

    fake = _FakeHostedClient(enabled=True, response={"trust_score": 5.0})
    _install_fake_hosted_client(monkeypatch, hosted_url="https://hosted.test", client=fake)

    metrics = reputation.compute_trust_metrics(agent_id)
    assert metrics["blended_global_weight"] == 0.0
    local_only = reputation._build_trust_metrics(
        agent_id=agent_id,
        total_calls=20, successful_calls=18, avg_latency_ms=800,
        rating_count=3, average_quality_rating=4.0,
        decay_multiplier=1.0,
    )["trust_score"]
    assert metrics["trust_score"] == pytest.approx(local_only, abs=0.01)


def test_compute_trust_metrics_silent_on_hosted_failure(isolated_db, monkeypatch):
    registry.init_db()
    reputation.init_reputation_db()
    agent_id = _register_agent("blend-fail")
    _set_agent_stats(isolated_db, agent_id, total_calls=2, successful_calls=2, avg_latency_ms=500)
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("UPDATE agents SET did = ? WHERE agent_id = ?", ("did:web:test:agents:z", agent_id))

    fake = _FakeHostedClient(enabled=True, response=None)  # simulate fetch failure
    _install_fake_hosted_client(monkeypatch, hosted_url="https://hosted.test", client=fake)

    metrics = reputation.compute_trust_metrics(agent_id)
    assert metrics["blended_global_weight"] == 0.0
    local_only = reputation._build_trust_metrics(
        agent_id=agent_id,
        total_calls=2, successful_calls=2, avg_latency_ms=500,
        rating_count=0, average_quality_rating=None,
        decay_multiplier=1.0,
    )["trust_score"]
    assert metrics["trust_score"] == pytest.approx(local_only, abs=0.01)


def test_compute_trust_metrics_skips_when_agent_has_no_did(isolated_db, monkeypatch):
    registry.init_db()
    reputation.init_reputation_db()
    agent_id = _register_agent("no-did")
    _set_agent_stats(isolated_db, agent_id, total_calls=2, successful_calls=2, avg_latency_ms=500)
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("UPDATE agents SET did = NULL WHERE agent_id = ?", (agent_id,))

    fake = _FakeHostedClient(enabled=True, response={"trust_score": 99.0})
    _install_fake_hosted_client(monkeypatch, hosted_url="https://hosted.test", client=fake)

    metrics = reputation.compute_trust_metrics(agent_id)
    # No DID → never call out, no blend.
    assert fake.fetch_calls == []
    assert metrics["blended_global_weight"] == 0.0


def test_blend_with_global_trust_pure_function():
    # local_weight = 10/20 = 0.5, global_weight = 0.5
    blended, gw = reputation._blend_with_global_trust(40.0, 80.0, 10)
    assert gw == pytest.approx(0.5)
    assert blended == pytest.approx(60.0)
    # Above threshold pins local_weight to 1.0
    blended, gw = reputation._blend_with_global_trust(40.0, 80.0, 50)
    assert gw == 0.0 and blended == 40.0
    # Missing global score → passthrough
    blended, gw = reputation._blend_with_global_trust(40.0, None, 0)
    assert gw == 0.0 and blended == 40.0
