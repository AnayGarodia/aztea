# OWNS: trust score computation, caller_ratings table (sole owner — no other module may write to it)
# NOT OWNS: dispute state transitions (disputes.py), payout clawbacks (payout_curve.py)
#
# INVARIANTS:
# - caller_ratings is ONLY written by this module — disputes.py reads it via helpers here
# - trust score is [0, 100]; new agents start at NEUTRAL (50) with low confidence
# - decay_multiplier shrinks score toward NEUTRAL (not zero) on inactivity — preserve this
#
# DECISIONS:
# - Bayesian average with prior (3.0 stars, weight 5.0) prevents wild swings on few ratings.
#   Can be tuned but the prior weight should stay > 0 or one bad rating tanks a new agent.
# - confidence_score = evidence / (evidence + 10) — converges slowly by design; fast convergence
#   would let bad actors game the score with a small burst of good jobs.
# - score formula: quality*0.45 + success*0.35 + latency*0.20 — weights are tunable,
#   but quality must remain the dominant factor.
#
# Trust score formula (for reference):
#   base  = quality*0.45 + success*0.35 + latency*0.20
#   score = NEUTRAL*(1-confidence) + base*confidence, then * decay_multiplier

import hashlib
import hmac
import logging
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from core import db as _db

_LOG = logging.getLogger(__name__)

# Fields in the rating push payload that contain a *local* identifier
# (user_id or job_id) and must be hashed before leaving this instance.
# Agent IDs (UUIDv5 from a public namespace) and the integer rating are
# safe to send raw — they are either public or non-sensitive.
_LOCAL_ID_FIELDS_TO_HASH = ("caller_owner_id", "agent_owner_id", "job_id")

DB_PATH = _db.DB_PATH
_local = _db._local

_QUALITY_WEIGHT = 0.40
_SUCCESS_WEIGHT = 0.45
_LATENCY_WEIGHT = 0.15
_NEUTRAL_TRUST = 0.5
_QUALITY_PRIOR_RATING = 3.0
_QUALITY_PRIOR_WEIGHT = 5.0
_LATENCY_HALF_SCORE_MS = 2000.0
# Confidence denominator was 10 — agents converged to their actual base
# score too slowly, so trust scores clustered near NEUTRAL (50) even after
# 30+ jobs. The 2026-05-08 eval scored the reputation surface D because of
# this. Lower denominator = trust converges to "what the agent actually
# delivers" faster, so a 22%-success agent reads as ~30 trust instead of
# ~48. Still high enough to need a real track record (5+ jobs) before the
# score moves materially.
_CONFIDENCE_DENOMINATOR = 6.0


def _conn() -> _db.DbConnection:
    """Return a thread-local SQLite connection with WAL mode."""
    return _db.get_raw_connection(DB_PATH)


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


def _normalize_decay_multiplier(value: float | int | None) -> float:
    parsed = _to_non_negative_float(value, default=1.0)
    return _clamp01(parsed if parsed > 0 else 1.0)


def _normalize_agent_stats(
    total_calls, successful_calls, avg_latency_ms
) -> tuple[int, int, float]:
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
        bounded_avg * rating_count + _QUALITY_PRIOR_RATING * _QUALITY_PRIOR_WEIGHT
    ) / (rating_count + _QUALITY_PRIOR_WEIGHT)
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


def _compute_trust_raw(
    quality_score: float, success_score: float, latency_score: float,
    confidence_score: float, decay_multiplier: float,
) -> float:
    """Pure: blend the per-axis scores into one ``trust_raw`` value in [0, 1].

    Why: confidence-weighted blend pulls new agents toward NEUTRAL until
    they accumulate evidence; the decay multiplier shrinks toward NEUTRAL
    on inactivity (never below the baseline floor) so silent agents do
    not spiral to zero.
    """
    base_score = (
        quality_score * _QUALITY_WEIGHT
        + success_score * _SUCCESS_WEIGHT
        + latency_score * _LATENCY_WEIGHT
    )
    trust_raw = (_NEUTRAL_TRUST * (1.0 - confidence_score)) + (base_score * confidence_score)
    baseline = _NEUTRAL_TRUST * (1.0 - confidence_score)
    return max(baseline, trust_raw * decay_multiplier)


def _shape_trust_metrics_dict(
    *, agent_id: str, trust_raw: float,
    quality_score: float, success_score: float, latency_score: float,
    confidence_score: float, rating_count: int,
    average_quality_rating: float | None,
    total_calls: int, successful_calls: int, avg_latency_ms: float,
    multiplier: float,
) -> dict:
    """Pure: shape the trust-metrics response dict; rounds for stable serialisation."""
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
        "decay_multiplier": round(multiplier, 6),
    }


def _build_trust_metrics(
    agent_id: str,
    total_calls: int,
    successful_calls: int,
    avg_latency_ms: float,
    rating_count: int,
    average_quality_rating: float | None,
    decay_multiplier: float = 1.0,
) -> dict:
    """Pure: trust score + per-axis sub-scores for one agent.

    Why: every consumer of trust (search ranking, auto-hire gating, MCP
    descriptions) reads from this single shape, so the score formula is
    audited in exactly one place.
    """
    quality_score = _compute_quality_score(average_quality_rating, rating_count)
    success_score = _compute_success_score(total_calls, successful_calls)
    latency_score = _compute_latency_score(total_calls, avg_latency_ms)
    confidence_score = _compute_confidence_score(total_calls, rating_count)
    multiplier = _normalize_decay_multiplier(decay_multiplier)
    trust_raw = _compute_trust_raw(
        quality_score, success_score, latency_score, confidence_score, multiplier,
    )
    return _shape_trust_metrics_dict(
        agent_id=agent_id, trust_raw=trust_raw,
        quality_score=quality_score, success_score=success_score,
        latency_score=latency_score, confidence_score=confidence_score,
        rating_count=rating_count, average_quality_rating=average_quality_rating,
        total_calls=total_calls, successful_calls=successful_calls,
        avg_latency_ms=avg_latency_ms, multiplier=multiplier,
    )


def init_reputation_db() -> None:
    """Create job quality rating tables and indexes if needed."""
    if _db.IS_POSTGRES:
        return
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS caller_ratings (
                rating_id         TEXT PRIMARY KEY,
                job_id            TEXT NOT NULL UNIQUE,
                caller_owner_id   TEXT NOT NULL,
                agent_owner_id    TEXT NOT NULL,
                rating            INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                comment           TEXT,
                created_at        TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_caller_ratings_caller_created ON caller_ratings(caller_owner_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_caller_ratings_agent_created ON caller_ratings(agent_owner_id, created_at DESC)"
        )


def _job_has_dispute_conn(conn: _db.DbConnection, job_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM disputes WHERE job_id = %s LIMIT 1",
            (job_id,),
        ).fetchone()
    except _db.OperationalError:
        return False
    return row is not None


def get_job_quality_rating(job_id: str) -> dict | None:
    init_reputation_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM job_quality_ratings WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def _load_job_for_quality_rating(conn: Any, job_id: str) -> dict[str, Any]:
    """Side-effect: fetch the job row used for quality-rating eligibility checks."""
    try:
        row = conn.execute(
            """
            SELECT job_id, agent_id, caller_owner_id, status
            FROM jobs
            WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
    except _db.OperationalError as e:
        raise RuntimeError(
            "jobs table is not initialized. Call jobs.init_jobs_db() first."
        ) from e
    if row is None:
        raise ValueError(f"Job '{job_id}' not found.")
    return dict(row)


def _check_quality_rating_eligible(
    conn: Any, job: dict[str, Any], caller_owner_id: str, job_id: str,
) -> None:
    """Pure-ish: enforce caller-ownership + completion + no-active-dispute invariants."""
    if job["status"] != "complete":
        raise ValueError("Only completed jobs can be rated.")
    if job["caller_owner_id"] != caller_owner_id:
        raise ValueError("Only the job caller can submit a quality rating.")
    if _job_has_dispute_conn(conn, job_id):
        raise ValueError("Ratings are locked once a dispute is filed.")


def _insert_quality_rating(
    conn: Any, *, job_id: str, job: dict[str, Any],
    caller_owner_id: str, rating: int,
) -> None:
    """Side-effect: write the job_quality_ratings row; raises ValueError on duplicate."""
    try:
        conn.execute(
            """
            INSERT INTO job_quality_ratings
                (job_id, agent_id, caller_owner_id, rating, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                job_id,
                job["agent_id"],
                caller_owner_id,
                rating,
                _now(),
            ),
        )
    except _db.IntegrityError as e:
        raise ValueError(f"Job '{job_id}' already has a quality rating.") from e


def record_job_quality_rating(job_id: str, caller_owner_id: str, rating: int) -> dict:
    """Side-effect: store a caller's quality rating for one delivered job.

    Why: one rating per job is enforced via the database UNIQUE constraint;
    duplicates surface as ``ValueError`` so the API layer can return 409
    rather than a 500 from the integrity violation.
    """
    init_reputation_db()
    validated_rating = _validate_rating(rating)
    with _conn() as conn:
        job = _load_job_for_quality_rating(conn, job_id)
        _check_quality_rating_eligible(conn, job, caller_owner_id, job_id)
        _insert_quality_rating(
            conn, job_id=job_id, job=job,
            caller_owner_id=caller_owner_id, rating=validated_rating,
        )
    created = get_job_quality_rating(job_id)
    if created:
        _push_rating_to_hosted_async(
            kind="quality",
            job_id=job_id,
            agent_id=created.get("agent_id"),
            caller_owner_id=caller_owner_id,
            rating=validated_rating,
        )
    return created if created else {}


def get_job_caller_rating(job_id: str) -> dict | None:
    init_reputation_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM caller_ratings WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def _load_job_for_rating(conn: Any, job_id: str) -> dict[str, Any]:
    """Side-effect: fetch the job row used for caller-rating eligibility checks."""
    try:
        row = conn.execute(
            """
            SELECT job_id, caller_owner_id, agent_owner_id, status
            FROM jobs
            WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
    except _db.OperationalError as e:
        raise RuntimeError(
            "jobs table is not initialized. Call jobs.init_jobs_db() first."
        ) from e
    if row is None:
        raise ValueError(f"Job '{job_id}' not found.")
    return dict(row)


def _check_caller_rating_eligible(
    conn: Any, job: dict[str, Any], agent_owner_id: str, job_id: str,
) -> None:
    """Pure-ish: enforce the four invariants required to record a caller rating.

    Why: ratings are locked once a dispute opens — this gate is the only
    place that decision is made, so dispute integrity stays single-sourced.
    """
    if job["status"] != "complete":
        raise ValueError("Only completed jobs can be rated.")
    if job["agent_owner_id"] != agent_owner_id:
        raise ValueError("Only the job's agent owner can rate this caller.")
    if _job_has_dispute_conn(conn, job_id):
        raise ValueError("Ratings are locked once a dispute is filed.")


def _insert_caller_rating(
    conn: Any, *, job_id: str, job: dict[str, Any],
    rating: int, comment: str | None,
) -> None:
    """Side-effect: write the caller_ratings row; raises ValueError on duplicate."""
    try:
        conn.execute(
            """
            INSERT INTO caller_ratings
                (rating_id, job_id, caller_owner_id, agent_owner_id, rating, comment, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                job_id,
                job["caller_owner_id"],
                job["agent_owner_id"],
                rating,
                comment,
                _now(),
            ),
        )
    except _db.IntegrityError as e:
        raise ValueError(f"Job '{job_id}' already has a caller rating.") from e


def record_caller_rating(
    job_id: str,
    agent_owner_id: str,
    rating: int,
    comment: str | None = None,
) -> dict:
    """Side-effect: record the agent's rating of the caller on one completed job.

    Why: exactly one caller rating is allowed per job — the database
    UNIQUE constraint enforces that, and we surface the violation as
    ``ValueError`` so the API layer returns 409 instead of 500.
    """
    validated_rating = _validate_rating(rating)
    init_reputation_db()
    normalized_comment = str(comment or "").strip() or None
    with _conn() as conn:
        job = _load_job_for_rating(conn, job_id)
        _check_caller_rating_eligible(conn, job, agent_owner_id, job_id)
        _insert_caller_rating(
            conn, job_id=job_id, job=job,
            rating=validated_rating, comment=normalized_comment,
        )
    created = get_job_caller_rating(job_id)
    if created:
        _push_rating_to_hosted_async(
            kind="caller",
            job_id=job_id,
            agent_owner_id=agent_owner_id,
            caller_owner_id=created.get("caller_owner_id"),
            rating=validated_rating,
        )
    return created if created else {}


def _get_agent_quality_summary_map(agent_ids: list[str]) -> dict[str, dict]:
    init_reputation_db()
    if not agent_ids:
        return {}

    placeholders = ",".join(["%s"] * len(agent_ids))
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
    """Return ``{rating_count, average_quality_rating}`` for a single agent."""
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

    placeholders = ",".join(["%s"] * len(agent_ids))
    query = f"""
        SELECT agent_id, total_calls, successful_calls, avg_latency_ms
        FROM agents
        WHERE agent_id IN ({placeholders})
    """

    with _conn() as conn:
        try:
            rows = conn.execute(query, tuple(agent_ids)).fetchall()
        except _db.OperationalError as e:
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


def _get_caller_quality_summary_map(caller_owner_ids: list[str]) -> dict[str, dict]:
    init_reputation_db()
    if not caller_owner_ids:
        return {}
    placeholders = ",".join(["%s"] * len(caller_owner_ids))
    query = f"""
        SELECT caller_owner_id, COUNT(*) AS rating_count, AVG(rating) AS average_rating
        FROM caller_ratings
        WHERE caller_owner_id IN ({placeholders})
        GROUP BY caller_owner_id
    """
    with _conn() as conn:
        rows = conn.execute(query, tuple(caller_owner_ids)).fetchall()

    summary: dict[str, dict] = {}
    for row in rows:
        summary[row["caller_owner_id"]] = {
            "rating_count": int(row["rating_count"] or 0),
            "average_rating": float(row["average_rating"])
            if row["average_rating"] is not None
            else None,
        }
    return summary


def _load_agent_stats(agent_id: str) -> tuple[int, int, float]:
    stats_map = _load_agent_stats_map([agent_id])
    stats = stats_map.get(agent_id)
    if stats is None:
        raise ValueError(f"Agent '{agent_id}' not found.")
    return stats


def _load_agent_decay_multiplier(agent_id: str) -> float:
    with _conn() as conn:
        row = conn.execute(
            "SELECT trust_decay_multiplier FROM agents WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"Agent '{agent_id}' not found.")
    return _normalize_decay_multiplier(row["trust_decay_multiplier"])


def compute_trust_metrics(agent_id: str) -> dict:
    """Compute trust metrics for one agent using registry stats + quality ratings."""
    total_calls, successful_calls, avg_latency_ms = _load_agent_stats(agent_id)
    decay_multiplier = _load_agent_decay_multiplier(agent_id)
    quality_summary = get_agent_quality_summary(agent_id)
    return _build_trust_metrics(
        agent_id=agent_id,
        total_calls=total_calls,
        successful_calls=successful_calls,
        avg_latency_ms=avg_latency_ms,
        rating_count=quality_summary["rating_count"],
        average_quality_rating=quality_summary["average_quality_rating"],
        decay_multiplier=decay_multiplier,
    )


def _load_dispute_rates_map(agent_ids: list[str]) -> dict[str, int]:
    """Return {agent_id: dispute_count} for each agent_id. Returns empty dict if tables missing."""
    if not agent_ids:
        return {}
    placeholders = ",".join(["%s"] * len(agent_ids))
    try:
        with _conn() as conn:
            rows = conn.execute(
                f"""
                SELECT j.agent_id, COUNT(d.dispute_id) AS dispute_count
                FROM disputes d
                JOIN jobs j ON d.job_id = j.job_id
                WHERE j.agent_id IN ({placeholders})
                GROUP BY j.agent_id
                """,
                agent_ids,
            ).fetchall()
        return {row["agent_id"]: int(row["dispute_count"]) for row in rows}
    except _db.OperationalError:
        return {}


def _load_client_quality_summary_map(
    agent_ids: list[str],
) -> dict[tuple[str, str], dict]:
    if not agent_ids:
        return {}
    placeholders = ",".join(["%s"] * len(agent_ids))
    try:
        with _conn() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    j.agent_id,
                    j.client_id,
                    COUNT(r.rating_id) AS rating_count,
                    AVG(r.rating) AS average_quality_rating
                FROM jobs j
                LEFT JOIN job_quality_ratings r ON r.job_id = j.job_id
                WHERE j.agent_id IN ({placeholders})
                  AND j.client_id IS NOT NULL
                  AND TRIM(j.client_id) != ''
                GROUP BY j.agent_id, j.client_id
                """,
                agent_ids,
            ).fetchall()
    except _db.OperationalError:
        return {}
    return {
        (str(row["agent_id"]), str(row["client_id"])): {
            "rating_count": int(row["rating_count"] or 0),
            "average_quality_rating": float(row["average_quality_rating"])
            if row["average_quality_rating"] is not None
            else None,
        }
        for row in rows
    }


def _build_client_stats_query(placeholders: str) -> str:
    """Pure: dialect-aware SQL for grouping job stats by (agent_id, client_id)."""
    latency_expr = (
        "EXTRACT(EPOCH FROM (completed_at::timestamptz - created_at::timestamptz)) * 1000.0"
        if _db.IS_POSTGRES
        else "(julianday(completed_at) - julianday(created_at)) * 86400000.0"
    )
    return f"""
        SELECT
            agent_id,
            client_id,
            COUNT(*) AS total_calls,
            SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS successful_calls,
            AVG(
                CASE
                    WHEN completed_at IS NOT NULL
                    THEN {latency_expr}
                    ELSE NULL
                END
            ) AS avg_latency_ms
        FROM jobs
        WHERE agent_id IN ({placeholders})
          AND client_id IS NOT NULL
          AND TRIM(client_id) != ''
        GROUP BY agent_id, client_id
    """


def _load_client_stats_map(
    agent_ids: list[str],
) -> dict[tuple[str, str], tuple[int, int, float]]:
    """Side-effect: per-(agent_id, client_id) call/latency stats; ``{}`` if jobs table missing."""
    if not agent_ids:
        return {}
    placeholders = ",".join(["%s"] * len(agent_ids))
    try:
        with _conn() as conn:
            rows = conn.execute(
                _build_client_stats_query(placeholders), agent_ids,
            ).fetchall()
    except _db.OperationalError:
        return {}
    return {
        (str(row["agent_id"]), str(row["client_id"])): _normalize_agent_stats(
            row["total_calls"],
            row["successful_calls"],
            row["avg_latency_ms"],
        )
        for row in rows
    }


def _build_client_trust_map(
    agent_ids: list[str], decay_by_agent: dict[str, float]
) -> dict[str, dict[str, float]]:
    stats_map = _load_client_stats_map(agent_ids)
    quality_map = _load_client_quality_summary_map(agent_ids)
    result: dict[str, dict[str, float]] = {}
    for (agent_id, client_id), stats in stats_map.items():
        quality = quality_map.get(
            (agent_id, client_id),
            {"rating_count": 0, "average_quality_rating": None},
        )
        metrics = _build_trust_metrics(
            agent_id=agent_id,
            total_calls=stats[0],
            successful_calls=stats[1],
            avg_latency_ms=stats[2],
            rating_count=int(quality["rating_count"] or 0),
            average_quality_rating=quality["average_quality_rating"],
            decay_multiplier=decay_by_agent.get(agent_id, 1.0),
        )
        result.setdefault(agent_id, {})[client_id] = metrics["trust_score"]
    return result


def enrich_agent_record(agent: dict) -> dict:
    """Side-effect: attach trust/reputation fields to a single registry agent record.

    Why: a thin wrapper around ``enrich_agent_records`` keeps the
    single-agent and batch paths byte-identical, so a future change to
    enrichment shape doesn't drift between the two.
    """
    if "agent_id" not in agent:
        raise ValueError("agent record must include agent_id")
    enriched = enrich_agent_records([agent])
    return enriched[0] if enriched else dict(agent)


def _resolve_agent_stats(
    agent: dict, stats_map: dict[str, tuple[int, int, float | None]],
) -> tuple[int, int, float | None]:
    """Pure: prefer fresh stats; fall back to per-row counts when the map misses."""
    stats = stats_map.get(agent["agent_id"])
    if stats is not None:
        return stats
    return _normalize_agent_stats(
        agent.get("total_calls"),
        agent.get("successful_calls"),
        agent.get("avg_latency_ms"),
    )


def _enrich_one_agent(
    agent: dict, *,
    summary_map: dict, stats_map: dict, dispute_counts: dict,
    client_trust_map: dict,
) -> dict:
    """Pure: attach trust/quality/dispute fields to one agent record."""
    if "agent_id" not in agent:
        raise ValueError("agent record must include agent_id")
    summary = summary_map.get(
        agent["agent_id"],
        {"rating_count": 0, "average_quality_rating": None},
    )
    total_calls, successful_calls, avg_latency_ms = _resolve_agent_stats(agent, stats_map)
    metrics = _build_trust_metrics(
        agent_id=agent["agent_id"],
        total_calls=total_calls,
        successful_calls=successful_calls,
        avg_latency_ms=avg_latency_ms,
        rating_count=summary["rating_count"],
        average_quality_rating=summary["average_quality_rating"],
        decay_multiplier=_normalize_decay_multiplier(
            agent.get("trust_decay_multiplier")
        ),
    )
    dc = dispute_counts.get(agent["agent_id"], 0)
    return {
        **agent,
        "trust_score": metrics["trust_score"],
        "quality_rating_count": metrics["rating_count"],
        "quality_rating_avg": metrics["average_quality_rating"],
        "confidence_score": metrics["confidence_score"],
        "reputation": metrics,
        "dispute_rate": round(dc / total_calls, 4) if total_calls > 0 else None,
        "by_client": client_trust_map.get(str(agent["agent_id"]), {}),
    }


def enrich_agent_records(agents: list[dict]) -> list[dict]:
    """Side-effect: attach trust/reputation fields to every agent record in one batch.

    Why: a single batch keeps the N database lookups bounded by N agents
    rather than N × per-row lookups in the caller.
    """
    if not agents:
        return []
    agent_ids = [a["agent_id"] for a in agents if "agent_id" in a]
    summary_map = _get_agent_quality_summary_map(agent_ids)
    stats_map = _load_agent_stats_map(agent_ids)
    dispute_counts = _load_dispute_rates_map(agent_ids)
    decay_by_agent = {
        str(agent["agent_id"]): _normalize_decay_multiplier(
            agent.get("trust_decay_multiplier")
        )
        for agent in agents
        if "agent_id" in agent
    }
    client_trust_map = _build_client_trust_map(agent_ids, decay_by_agent)
    return [
        _enrich_one_agent(
            agent,
            summary_map=summary_map, stats_map=stats_map,
            dispute_counts=dispute_counts, client_trust_map=client_trust_map,
        )
        for agent in agents
    ]


def rank_agents_by_trust(agents: list[dict], descending: bool = True) -> list[dict]:
    """Return enriched agents sorted by trust_score."""
    enriched = enrich_agent_records(agents)
    return sorted(
        enriched, key=lambda item: item.get("trust_score", 0.0), reverse=descending
    )


def compute_caller_trust_metrics(caller_owner_id: str) -> dict:
    """
    Compute caller trust based on bilateral caller ratings, using the same
    Bayesian prior used for agent quality.
    """
    normalized_owner_id = str(caller_owner_id or "").strip()
    if not normalized_owner_id:
        raise ValueError("caller_owner_id must be a non-empty string.")

    summary = _get_caller_quality_summary_map([normalized_owner_id]).get(
        normalized_owner_id,
        {"rating_count": 0, "average_rating": None},
    )
    rating_count = int(summary["rating_count"])
    average_rating = summary["average_rating"]
    quality_score = _compute_quality_score(average_rating, rating_count)
    confidence = _clamp01(rating_count / (rating_count + _QUALITY_PRIOR_WEIGHT))
    trust_raw = (_NEUTRAL_TRUST * (1.0 - confidence)) + (quality_score * confidence)
    return {
        "caller_owner_id": normalized_owner_id,
        "trust_score": round(trust_raw * 100.0, 2),
        "trust_score_normalized": round(trust_raw, 6),
        "quality_score": round(quality_score, 4),
        "rating_count": rating_count,
        "average_rating": round(float(average_rating), 4)
        if average_rating is not None
        else None,
        "confidence_score": round(confidence, 4),
    }


def count_caller_given_ratings(
    caller_owner_id: str, *, rating: int | None = None
) -> int:
    """Count how many ratings a caller has submitted; optionally filter by a specific ``rating`` value.

    Used for abuse detection (e.g. detecting a caller who exclusively gives 1-star ratings).
    """
    normalized_owner_id = str(caller_owner_id or "").strip()
    if not normalized_owner_id:
        return 0
    with _conn() as conn:
        if rating is None:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM job_quality_ratings
                WHERE caller_owner_id = %s
                """,
                (normalized_owner_id,),
            ).fetchone()
        else:
            validated = _validate_rating(int(rating))
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM job_quality_ratings
                WHERE caller_owner_id = %s AND rating = %s
                """,
                (normalized_owner_id, validated),
            ).fetchone()
    return int(row["count"] if row else 0)


def _instance_hmac_key() -> bytes:
    """Per-instance HMAC key used to anonymise local IDs before leaving the box.

    Resolution order (call-time):
      1. ``AZTEA_INSTANCE_SALT`` — explicit operator-set value (preferred).
      2. SHA-256 of ``API_KEY`` — derived, stable as long as the master key
         is stable. Rotating ``API_KEY`` rotates the salt, which is fine:
         the hosted side just sees a fresh set of opaque IDs.
      3. ``b"aztea-oss-default-salt"`` — last-resort. Logs a warning
         because it means cross-instance hashes collide; not catastrophic
         (federated trust is additive) but worth knowing.
    """
    explicit = os.environ.get("AZTEA_INSTANCE_SALT", "").strip()
    if explicit:
        return explicit.encode("utf-8")
    api_key = os.environ.get("API_KEY", "").strip()
    if api_key:
        return hashlib.sha256(api_key.encode("utf-8")).digest()
    _LOG.warning(
        "reputation: no AZTEA_INSTANCE_SALT or API_KEY set; using default salt — "
        "hosted hashes will collide with other unconfigured instances."
    )
    return b"aztea-oss-default-salt"


def _instance_hash(local_id: object) -> str:
    """HMAC-SHA256 a local identifier to a 16-hex-char opaque token.

    The hosted side cannot reverse this back to the original ID without the
    instance salt. Same input → same output within an instance, which is
    what the federated trust cache needs to dedupe ratings from one user.
    """
    if local_id is None:
        return ""
    raw = str(local_id).encode("utf-8")
    digest = hmac.new(_instance_hmac_key(), raw, hashlib.sha256).hexdigest()
    return digest[:16]


def _redact_rating_payload(rating: dict[str, object]) -> dict[str, object]:
    """Return a copy of ``rating`` with local IDs replaced by HMAC hashes.

    The shape is preserved — fields keep their names and approximate
    string-shape — so the hosted server can dedupe without learning the
    original user_id or job_id.
    """
    redacted: dict[str, object] = {}
    for key, value in rating.items():
        if key in _LOCAL_ID_FIELDS_TO_HASH:
            redacted[key] = _instance_hash(value) if value is not None else None
        else:
            redacted[key] = value
    return redacted


def _push_rating_to_hosted_async(**rating: object) -> None:
    """Best-effort push of a rating to aztea.ai's federated reputation cache.

    No-ops in OSS-mode (AZTEA_HOSTED_API_URL unset). Runs in a daemon
    thread so the request returns immediately. Failures are logged at
    debug level — the local rating row is the source of truth and
    federated trust is purely additive.

    PRIVACY: every local identifier (caller_owner_id, agent_owner_id,
    job_id) is HMAC-hashed with an instance-scoped salt before being sent.
    The hosted server can dedupe ratings from the same user but cannot
    re-identify the user. ``agent_id`` (UUIDv5 from a public namespace) is
    sent raw because the federated network needs it to attribute ratings
    to the right agent.
    """
    payload = _redact_rating_payload(dict(rating))

    def _send() -> None:
        try:
            from core.hosted_client import get_hosted_client

            client = get_hosted_client()
            if not client.is_enabled():
                return
            client.push_rating(payload)
        except Exception as exc:  # noqa: BLE001 — fire-and-forget, must not raise
            _LOG.debug("reputation: hosted push failed: %s", exc)

    threading.Thread(target=_send, daemon=True).start()
