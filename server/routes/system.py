"""
System routes: health and versioned diagnostics (small surface, safe to own separately).
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core import models as core_models
from core import jobs
from core import registry
from core.openapi_responses import pick_error_responses

router = APIRouter()


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
        503: {"model": core_models.ErrorResponse, "description": "One or more checks failed."},
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

    agent_count = len(registry.get_agents())
    status = "ok" if all_ok else "degraded"
    response = core_models.HealthResponse(
        status=status,
        checks=checks,
        agent_count=agent_count,
        version=_read_version(),
        agents=agent_count,
    )

    if not all_ok:
        return JSONResponse(status_code=503, content=response.model_dump())
    return response
