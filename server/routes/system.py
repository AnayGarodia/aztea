"""
System routes: health and versioned diagnostics (small surface, safe to own separately).
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core import jobs, registry
from core import feature_flags as _feature_flags
from core import models as core_models
from core.openapi_responses import pick_error_responses
from server.builtin_agents import constants as _builtin_constants

router = APIRouter()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Defaults mirror the values declared in server/application_parts/part_000.py
# so this endpoint reflects the same policy the dispute-creation route applies.
_DEFAULT_DISPUTE_FILING_DEPOSIT_BPS = 500
_DEFAULT_DISPUTE_FILING_DEPOSIT_MIN_CENTS = 25
_DEFAULT_JOB_DISPUTE_WINDOW_HOURS = 72
_DISPUTE_JUDGES_REQUIRED = 2
_DISPUTE_JUDGES_TOTAL = 3


def _read_version() -> str:
    try:
        version_path = os.path.join(os.path.dirname(__file__), "..", "..", "VERSION")
        return open(version_path, encoding="utf-8").read().strip()
    except OSError:
        return "unknown"


def _db_path_writable(db_path: str) -> bool:
    """True if the DB file exists and is writable, or the parent directory can create it."""
    path = os.path.abspath(db_path)
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    parent = os.path.dirname(path) or "."
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


@router.get(
    "/health",
    response_model=core_models.HealthResponse,
    responses={
        200: {"description": "All checks passed."},
        503: {
            "model": core_models.ErrorResponse,
            "description": "One or more checks failed.",
        },
        **pick_error_responses(429, 500),
    },
)
def health() -> core_models.HealthResponse:
    try:
        import psutil as _psutil
    except ImportError:
        _psutil = None

    checks: dict[str, core_models.HealthCheckDetail] = {}
    all_ok = True

    # DB check
    try:
        t0 = time.monotonic()
        with jobs._conn() as conn:
            conn.execute("SELECT 1").fetchone()
        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        checks["db"] = core_models.HealthCheckDetail(ok=True, latency_ms=latency_ms)
    except Exception as exc:
        all_ok = False
        checks["db"] = core_models.HealthCheckDetail(ok=False, error=str(exc))

    # Disk check (directory must allow creating the DB, not just the path string)
    try:
        writable = _db_path_writable(jobs.DB_PATH)
        checks["disk"] = core_models.HealthCheckDetail(ok=writable, writable=writable)
        if not writable:
            all_ok = False
    except OSError as exc:
        all_ok = False
        checks["disk"] = core_models.HealthCheckDetail(ok=False, error=str(exc))

    # Memory check (optional — requires psutil)
    if _psutil is not None:
        try:
            proc = _psutil.Process()
            rss_mb = round(proc.memory_info().rss / (1024 * 1024), 2)
            checks["memory"] = core_models.HealthCheckDetail(ok=True, rss_mb=rss_mb)
        except Exception as exc:
            all_ok = False
            checks["memory"] = core_models.HealthCheckDetail(ok=False, error=str(exc))

    # 1.7.3 — count must match /registry/agents (the canonical discovery
    # surface). Pre-1.7.3 /health applied a stricter filter (active OR
    # curated_public) than /registry/agents (sunset-only), which let the
    # 1.7.1 eval observe an 8-row drift (38 vs 46). Apply the same filter
    # /registry/agents uses: exclude sunset, include everything else
    # public, no status='active' gate (probation agents are still publicly
    # listed and are counted by every other surface).
    sunset_ids = set(_builtin_constants.SUNSET_DEPRECATED_AGENT_IDS)
    public_agents = [
        agent
        for agent in registry.get_agents(include_internal=False)
        if str(agent.get("agent_id") or "") not in sunset_ids
        and str(agent.get("review_status") or "").strip().lower() != "sunset"
    ]
    agent_count = len(public_agents)
    status = "ok" if all_ok else "degraded"
    # 1.7.2 — N15 observability: surface the resolved AZTEA_RESULT_CACHE_V2
    # value so anyone can detect a cache-off prod from outside the host.
    # None == enabled (default); a non-None string is the disable reason.
    _cache_disabled_reason: str | None = None
    if not _feature_flags.RESULT_CACHE_V2:
        _cache_disabled_reason = (
            "AZTEA_RESULT_CACHE_V2 is set to a falsy value in this process. "
            "Identical-input calls will NOT hit the cache; every call will "
            "settle a fresh charge."
        )
    response = core_models.HealthResponse(
        status=status,
        checks=checks,
        agent_count=agent_count,
        version=_read_version(),
        agents=agent_count,
        result_cache_disabled_reason=_cache_disabled_reason,
    )

    if not all_ok:
        return JSONResponse(status_code=503, content=response.model_dump())
    return response


# 2026-05-19 (B7): /system/health alias. The platform's own dispute policy
# docstring (part_005.py) and several runbooks reference this URL as the
# canonical health endpoint, but the only registered route was /health.
# GET /system/health used to fall through the SPA catch-all and return
# React index.html — a confusing first impression for an integrator
# checking health from an intuitive path. Now /system/health is a thin
# wrapper around the same handler so the two paths return identical
# bodies and status codes.
@router.get(
    "/system/health",
    response_model=core_models.HealthResponse,
    responses={
        200: {"description": "All checks passed."},
        503: {
            "model": core_models.ErrorResponse,
            "description": "One or more checks failed.",
        },
        **pick_error_responses(429, 500),
    },
    summary="Alias for /health — same body, same status codes.",
)
def system_health() -> core_models.HealthResponse:
    return health()


@router.get(
    "/ops/dispute-policy",
    responses=pick_error_responses(429, 500),
    summary="Public read-only view of the dispute filing policy.",
)
def ops_dispute_policy() -> JSONResponse:
    """Filing-deposit formula and judge-panel shape used when filing a dispute.

    Public so CLI / SDK clients can quote the exact deposit amount before the
    user confirms. No auth required: these are policy constants, not secrets.
    """
    bps = _env_int("DISPUTE_FILING_DEPOSIT_BPS", _DEFAULT_DISPUTE_FILING_DEPOSIT_BPS)
    min_cents = _env_int(
        "DISPUTE_FILING_DEPOSIT_MIN_CENTS",
        _DEFAULT_DISPUTE_FILING_DEPOSIT_MIN_CENTS,
    )
    window_hours = _env_int(
        "DEFAULT_JOB_DISPUTE_WINDOW_HOURS",
        _DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
    )
    return JSONResponse(
        content={
            "filing_deposit_bps": bps,
            "filing_deposit_min_cents": min_cents,
            "default_dispute_window_hours": window_hours,
            "judges_required": _DISPUTE_JUDGES_REQUIRED,
            "judges_total": _DISPUTE_JUDGES_TOTAL,
            "formula": (
                "deposit_cents = max(filing_deposit_min_cents, "
                "price_cents * filing_deposit_bps / 10000)"
            ),
        }
    )
