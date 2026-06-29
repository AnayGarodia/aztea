"""
otto_telemetry.py — HTTP surface for Otto's anonymous product telemetry.

Routes (mounted in part_011, before the SPA catch-all so the GET routes aren't
shadowed by it):

  POST /otto/telemetry            ingest a batch of events. Auth = the Otto proxy
                                  bearer (same as /otto/responses). Append-only,
                                  deduped by event_id.

  GET  /admin/otto/metrics        the dashboard data. Admin-scope + IP allowlist.
                                  ?section=<one of metrics.SECTIONS>&window=7d|30d|90d
                                  Omit section to get every section in one payload.

  GET  /otto/download             website download button. Records one anonymous
                                  `download` event, then 302-redirects to the DMG.
                                  Public (no auth) — it's the marketing funnel
                                  entry. Separate from the Sparkle auto-update
                                  path (which hits the raw .dmg/appcast directly
                                  and is NOT counted as a new download).

Auth helpers are injected by the caller (create_router) because they live in the
sharded server.application namespace; importing them here would cycle.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from core import otto_telemetry as _tel

logger = logging.getLogger(__name__)


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""


def _dmg_redirect_target() -> str | None:
    """Where the website download button points. Configurable so a release bump
    doesn't need a code change. Unset → the route 503s with a clear message."""
    return (os.environ.get("OTTO_DMG_URL") or "").strip() or None


def create_router(
    *,
    require_api_key: Callable[..., Any],
    require_scope: Callable[..., None],
    require_admin_ip_allowlist: Callable[[Request], None],
    otto_proxy_auth_ok: Callable[[str], bool],
    limiter: Any,
) -> APIRouter:
    router = APIRouter()

    def _admin_gate(caller: Any, request: Request) -> None:
        require_scope(caller, "admin", detail="This endpoint requires admin scope.")
        require_admin_ip_allowlist(request)

    # ── Ingest ──────────────────────────────────────────────────────────────
    @router.post("/otto/telemetry")
    @limiter.limit("240/minute")
    def ingest(request: Request, body: dict = Body(...)) -> JSONResponse:
        token = _bearer(request)
        if not otto_proxy_auth_ok(token):
            raise HTTPException(status_code=401, detail="Invalid Otto app token.")
        if not isinstance(body, dict) or "events" not in body:
            raise HTTPException(
                status_code=400,
                detail="Body must be {\"events\": [...]} per docs/otto-telemetry-schema.md.",
            )
        result = _tel.ingest_events(body.get("events") or [])
        # 207-ish semantics in a 200: the client reads accepted/duplicate/rejected
        # to advance its send-queue cursor. A partial reject is not a transport error.
        return JSONResponse(status_code=200, content=result.as_dict())

    # ── Dashboard metrics ───────────────────────────────────────────────────
    @router.get("/admin/otto/metrics")
    def metrics(
        request: Request,
        section: str | None = Query(None),
        window: str = Query("30d"),
        caller: Any = Depends(require_api_key),
    ) -> JSONResponse:
        _admin_gate(caller, request)
        if section is None:
            from core.otto_telemetry.metrics import compute_all

            return JSONResponse(content={"window": window, "sections": compute_all(window)})
        try:
            data = _tel.compute_section(section, window)
        except KeyError:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown section {section!r}. Allowed: {sorted(_tel.SECTIONS)}",
            )
        return JSONResponse(content={"section": section, "window": window, "data": data})

    # ── Website download (counted) ──────────────────────────────────────────
    @router.get("/otto/download")
    @limiter.limit("120/minute")
    def download(
        request: Request,
        platform: str = Query("mac"),
        utm_source: str | None = Query(None),
        utm_campaign: str | None = Query(None),
    ) -> RedirectResponse:
        target = _dmg_redirect_target()
        if not target:
            raise HTTPException(
                status_code=503,
                detail="Download is not configured (OTTO_DMG_URL unset).",
            )
        _tel.record_download(
            platform=platform,
            referrer=request.headers.get("Referer"),
            utm_source=utm_source,
            utm_campaign=utm_campaign,
        )
        return RedirectResponse(url=target, status_code=302)

    return router
