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


def test_record_job_quality_rating_requires_completed_job_and_prevents_duplicates(isolated_db):
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

    with pytest.raises(ValueError, match="already has a quality rating"):
        reputation.record_job_quality_rating(job["job_id"], caller_owner_id, 4)


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
