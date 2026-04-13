"""
reputation.py — SQLite-backed reputation and trust-score primitives for agentmarket.

Stores one caller quality rating (1-5) per completed job and computes trust
metrics from quality + success rate + latency + confidence(volume).
"""

import math
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "registry.db")
_local = threading.local()

_QUALITY_WEIGHT = 0.45
_SUCCESS_WEIGHT = 0.35
_LATENCY_WEIGHT = 0.20
_NEUTRAL_TRUST = 0.5
_QUALITY_PRIOR_RATING = 3.0
_QUALITY_PRIOR_WEIGHT = 5.0
_LATENCY_HALF_SCORE_MS = 2000.0
_CONFIDENCE_DENOMINATOR = 10.0


def _conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL mode."""
    if not getattr(_local, "conn", None):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _to_non_negative_int(value, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _to_non_negative_float(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed < 0:
        return default
    return parsed


def _normalize_agent_stats(total_calls, successful_calls, avg_latency_ms) -> tuple[int, int, float]:
    total = _to_non_negative_int(total_calls, default=0)
    successful = _to_non_negative_int(successful_calls, default=0)
    if successful > total:
        successful = total
    latency = _to_non_negative_float(avg_latency_ms, default=0.0)
    return total, successful, latency


def _validate_rating(rating: int) -> int:
    if isinstance(rating, bool) or not isinstance(rating, int):
        raise ValueError("rating must be an integer between 1 and 5.")
    if rating < 1 or rating > 5:
        raise ValueError("rating must be between 1 and 5.")
    return rating


def _compute_quality_score(avg_rating: float | None, rating_count: int) -> float:
    if rating_count <= 0 or avg_rating is None:
        return (_QUALITY_PRIOR_RATING - 1.0) / 4.0
    bounded_avg = min(5.0, max(1.0, float(avg_rating)))
    bayesian_avg = (
        (bounded_avg * rating_count + _QUALITY_PRIOR_RATING * _QUALITY_PRIOR_WEIGHT)
        / (rating_count + _QUALITY_PRIOR_WEIGHT)
    )
    return _clamp01((bayesian_avg - 1.0) / 4.0)


def _compute_success_score(total_calls: int, successful_calls: int) -> float:
    return _clamp01((successful_calls + 1.0) / (total_calls + 2.0))


def _compute_latency_score(total_calls: int, avg_latency_ms: float) -> float:
    if total_calls <= 0:
        return _NEUTRAL_TRUST
    return _clamp01(1.0 / (1.0 + (avg_latency_ms / _LATENCY_HALF_SCORE_MS)))


def _compute_confidence_score(total_calls: int, rating_count: int) -> float:
    evidence = total_calls + (rating_count * 2)
    if evidence <= 0:
        return 0.0
    return _clamp01(evidence / (evidence + _CONFIDENCE_DENOMINATOR))


def _build_trust_metrics(
    agent_id: str,
    total_calls: int,
    successful_calls: int,
    avg_latency_ms: float,
    rating_count: int,
    average_quality_rating: float | None,
) -> dict:
    quality_score = _compute_quality_score(average_quality_rating, rating_count)
    success_score = _compute_success_score(total_calls, successful_calls)
    latency_score = _compute_latency_score(total_calls, avg_latency_ms)
    confidence_score = _compute_confidence_score(total_calls, rating_count)

    base_score = (
        quality_score * _QUALITY_WEIGHT
        + success_score * _SUCCESS_WEIGHT
        + latency_score * _LATENCY_WEIGHT
    )
    trust_raw = (_NEUTRAL_TRUST * (1.0 - confidence_score)) + (base_score * confidence_score)
    success_rate = (successful_calls / total_calls) if total_calls > 0 else None

    return {
        "agent_id": agent_id,
        "trust_score": round(trust_raw * 100.0, 2),
        "quality_score": round(quality_score, 4),
        "success_score": round(success_score, 4),
        "latency_score": round(latency_score, 4),
        "confidence_score": round(confidence_score, 4),
        "rating_count": rating_count,
        "average_quality_rating": (
            round(float(average_quality_rating), 4)
            if average_quality_rating is not None
            else None
        ),
        "total_calls": total_calls,
        "successful_calls": successful_calls,
        "success_rate": round(success_rate, 4) if success_rate is not None else None,
        "avg_latency_ms": round(avg_latency_ms, 3),
    }


def init_reputation_db() -> None:
    """Create job quality rating tables and indexes if needed."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_quality_ratings (
                rating_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id           TEXT NOT NULL UNIQUE,
                agent_id         TEXT NOT NULL,
                caller_owner_id  TEXT NOT NULL,
                rating           INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
                created_at       TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_quality_agent ON job_quality_ratings(agent_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_quality_caller ON job_quality_ratings(caller_owner_id, created_at DESC)"
        )


def get_job_quality_rating(job_id: str) -> dict | None:
    init_reputation_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM job_quality_ratings WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def record_job_quality_rating(job_id: str, caller_owner_id: str, rating: int) -> dict:
    """
    Store a caller quality rating for one delivered job.
    Enforces one rating per job and validates caller ownership.
    """
    init_reputation_db()
    validated_rating = _validate_rating(rating)

    with _conn() as conn:
        try:
            job = conn.execute(
                """
                SELECT job_id, agent_id, caller_owner_id, status
                FROM jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                "jobs table is not initialized. Call jobs.init_jobs_db() first."
            ) from e

        if job is None:
            raise ValueError(f"Job '{job_id}' not found.")
        if job["status"] != "complete":
            raise ValueError("Only completed jobs can be rated.")
        if job["caller_owner_id"] != caller_owner_id:
            raise ValueError("Only the job caller can submit a quality rating.")

        try:
            conn.execute(
                """
                INSERT INTO job_quality_ratings
                    (job_id, agent_id, caller_owner_id, rating, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job["agent_id"],
                    caller_owner_id,
                    validated_rating,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"Job '{job_id}' already has a quality rating.") from e

    created = get_job_quality_rating(job_id)
    return created if created else {}


def _get_agent_quality_summary_map(agent_ids: list[str]) -> dict[str, dict]:
    init_reputation_db()
    if not agent_ids:
        return {}

    placeholders = ",".join(["?"] * len(agent_ids))
    query = f"""
        SELECT agent_id, COUNT(*) AS rating_count, AVG(rating) AS average_quality_rating
        FROM job_quality_ratings
        WHERE agent_id IN ({placeholders})
        GROUP BY agent_id
    """

    with _conn() as conn:
        rows = conn.execute(query, tuple(agent_ids)).fetchall()

    summary: dict[str, dict] = {}
    for row in rows:
        summary[row["agent_id"]] = {
            "rating_count": int(row["rating_count"] or 0),
            "average_quality_rating": (
                float(row["average_quality_rating"])
                if row["average_quality_rating"] is not None
                else None
            ),
        }
    return summary


def get_agent_quality_summary(agent_id: str) -> dict:
    summary_map = _get_agent_quality_summary_map([agent_id])
    summary = summary_map.get(
        agent_id,
        {"rating_count": 0, "average_quality_rating": None},
    )
    return {
        "agent_id": agent_id,
        "rating_count": summary["rating_count"],
        "average_quality_rating": (
            round(summary["average_quality_rating"], 4)
            if summary["average_quality_rating"] is not None
            else None
        ),
    }


def _load_agent_stats_map(agent_ids: list[str]) -> dict[str, tuple[int, int, float]]:
    if not agent_ids:
        return {}

    placeholders = ",".join(["?"] * len(agent_ids))
    query = f"""
        SELECT agent_id, total_calls, successful_calls, avg_latency_ms
        FROM agents
        WHERE agent_id IN ({placeholders})
    """

    with _conn() as conn:
        try:
            rows = conn.execute(query, tuple(agent_ids)).fetchall()
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                "agents table is not initialized. Call registry.init_db() first."
            ) from e

    stats_map: dict[str, tuple[int, int, float]] = {}
    for row in rows:
        stats_map[row["agent_id"]] = _normalize_agent_stats(
            row["total_calls"],
            row["successful_calls"],
            row["avg_latency_ms"],
        )
    return stats_map


def _load_agent_stats(agent_id: str) -> tuple[int, int, float]:
    stats_map = _load_agent_stats_map([agent_id])
    stats = stats_map.get(agent_id)
    if stats is None:
        raise ValueError(f"Agent '{agent_id}' not found.")
    return stats


def compute_trust_metrics(agent_id: str) -> dict:
    """Compute trust metrics for one agent using registry stats + quality ratings."""
    total_calls, successful_calls, avg_latency_ms = _load_agent_stats(agent_id)
    quality_summary = get_agent_quality_summary(agent_id)
    return _build_trust_metrics(
        agent_id=agent_id,
        total_calls=total_calls,
        successful_calls=successful_calls,
        avg_latency_ms=avg_latency_ms,
        rating_count=quality_summary["rating_count"],
        average_quality_rating=quality_summary["average_quality_rating"],
    )


def enrich_agent_record(agent: dict) -> dict:
    """Attach trust/reputation fields to a single registry agent record."""
    if "agent_id" not in agent:
        raise ValueError("agent record must include agent_id")

    agent_id = agent["agent_id"]
    summary_map = _get_agent_quality_summary_map([agent_id])
    stats_map = _load_agent_stats_map([agent_id])
    summary = summary_map.get(
        agent_id,
        {"rating_count": 0, "average_quality_rating": None},
    )
    stats = stats_map.get(agent_id)
    if stats is None:
        stats = _normalize_agent_stats(
            agent.get("total_calls"),
            agent.get("successful_calls"),
            agent.get("avg_latency_ms"),
        )
    total_calls, successful_calls, avg_latency_ms = stats

    metrics = _build_trust_metrics(
        agent_id=agent_id,
        total_calls=total_calls,
        successful_calls=successful_calls,
        avg_latency_ms=avg_latency_ms,
        rating_count=summary["rating_count"],
        average_quality_rating=summary["average_quality_rating"],
    )

    enriched = dict(agent)
    enriched["trust_score"] = metrics["trust_score"]
    enriched["quality_rating_count"] = metrics["rating_count"]
    enriched["quality_rating_avg"] = metrics["average_quality_rating"]
    enriched["confidence_score"] = metrics["confidence_score"]
    enriched["reputation"] = metrics
    return enriched


def enrich_agent_records(agents: list[dict]) -> list[dict]:
    """Attach trust/reputation fields to every agent record in one batch."""
    if not agents:
        return []

    agent_ids = [a["agent_id"] for a in agents if "agent_id" in a]
    summary_map = _get_agent_quality_summary_map(agent_ids)
    stats_map = _load_agent_stats_map(agent_ids)

    enriched_records = []
    for agent in agents:
        if "agent_id" not in agent:
            raise ValueError("agent record must include agent_id")

        summary = summary_map.get(
            agent["agent_id"],
            {"rating_count": 0, "average_quality_rating": None},
        )
        stats = stats_map.get(agent["agent_id"])
        if stats is None:
            stats = _normalize_agent_stats(
                agent.get("total_calls"),
                agent.get("successful_calls"),
                agent.get("avg_latency_ms"),
            )
        total_calls, successful_calls, avg_latency_ms = stats
        metrics = _build_trust_metrics(
            agent_id=agent["agent_id"],
            total_calls=total_calls,
            successful_calls=successful_calls,
            avg_latency_ms=avg_latency_ms,
            rating_count=summary["rating_count"],
            average_quality_rating=summary["average_quality_rating"],
        )

        enriched = dict(agent)
        enriched["trust_score"] = metrics["trust_score"]
        enriched["quality_rating_count"] = metrics["rating_count"]
        enriched["quality_rating_avg"] = metrics["average_quality_rating"]
        enriched["confidence_score"] = metrics["confidence_score"]
        enriched["reputation"] = metrics
        enriched_records.append(enriched)
    return enriched_records


def rank_agents_by_trust(agents: list[dict], descending: bool = True) -> list[dict]:
    """Return enriched agents sorted by trust_score."""
    enriched = enrich_agent_records(agents)
    return sorted(enriched, key=lambda item: item.get("trust_score", 0.0), reverse=descending)
