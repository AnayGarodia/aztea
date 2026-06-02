"""Public, Firecrawl-shaped web API (Phase D) + the public verify endpoint (Phase F).

# OWNS: the developer-facing HTTP surface — POST /scrape /map /crawl /extract (one
#        engine, Firecrawl-compatible response shape) and POST /web/verify (offline
#        receipt verification, the provenance differentiator).
# NOT OWNS: the engine (agents.site_navigator, core.web.sitemap/crawl), SSRF policy
#           (the engine validates every URL), or receipt signing (core.observation_receipts).
# INVARIANTS:
#   * /scrape /map /crawl /extract are gated by AZTEA_WEB_API_ENABLED (default OFF) —
#     they make outbound fetches, an abuse surface, so the off path is the default.
#   * Anonymous-callable + IP-rate-limited (the zero-auth demo tier, review fix #12).
#   * Every outbound URL is SSRF-validated inside the engine — the routes add nothing
#     that bypasses it.
#   * /web/verify is ALWAYS on (no outbound, no money) — it only checks a signature.
# DECISIONS:
#   * Factory pattern matches server/routes/playground.py + public_integrations.py so the
#     part_*.py shards stay under the line budget.
#   * Response shape is Firecrawl-compatible ({success, data:{markdown,html,links,json,
#     metadata}}) so a developer can point a Firecrawl client at this base URL (review T2).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from agents import site_navigator as _site_navigator
from core import error_codes, observation_receipts
from core.web import crawl as _crawl
from core.web import sitemap as _sitemap

_LOG = logging.getLogger(__name__)

_WEB_API_RATE_LIMIT = "10/minute"
_MAX_URL_CHARS = 2_000
_CRAWL_LIMIT_HARD = 25  # public crawl is tightly bounded (worker-pool protection); crawl_site
                        # also enforces a total wall-clock budget so one call can't pin a worker


def _web_api_enabled() -> bool:
    """Pure: master gate for the outbound-fetch endpoints (default OFF)."""
    return os.environ.get("AZTEA_WEB_API_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _disabled_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=error_codes.make_error(
            "web_api.disabled",
            "The public web API is disabled on this Aztea instance. "
            "Set AZTEA_WEB_API_ENABLED=1 to enable /scrape /map /crawl /extract.",
        ),
    )


def _require_url(body: dict[str, Any]) -> str:
    """Pure: extract + bound-check the url, raising a structured 422 on failure."""
    url = body.get("url")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(error_codes.INVALID_INPUT, "url is required."),
        )
    if len(url) > _MAX_URL_CHARS:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error("web_api.url_too_long", f"url exceeds {_MAX_URL_CHARS} chars."),
        )
    return url.strip()


def _firecrawl_scrape_data(nav_out: dict[str, Any]) -> dict[str, Any]:
    """Pure: translate a site_navigator result into Firecrawl-compatible scrape data.

    {markdown, html, links, json, metadata:{title,sourceURL,statusCode}} so a Firecrawl
    client reads it unchanged. The Aztea-native extras (signed receipt, discovered API,
    cost_class) are passed through under their own keys — additive, not breaking.
    """
    site_map = nav_out.get("site_map") or {}
    data: dict[str, Any] = {
        "markdown": nav_out.get("markdown"),
        "html": nav_out.get("html"),
        "links": nav_out.get("links"),
        "json": nav_out.get("result"),
        "metadata": {
            "title": site_map.get("title"),
            "sourceURL": nav_out.get("requested_url"),
            "url": nav_out.get("url"),
            "statusCode": 200,
        },
    }
    # Aztea-native extras (Firecrawl has no equivalent) — additive.
    for key in ("observation_receipt", "site_map", "source", "cost_class", "reuse"):
        if key in nav_out:
            data[f"aztea_{key}" if key in ("source", "reuse") else key] = nav_out[key]
    return data


def _scrape_or_error(payload: dict[str, Any]) -> JSONResponse:
    """Run the navigator and shape a Firecrawl-style {success,data}|{success:false,error}."""
    out = _site_navigator.run(payload)
    if isinstance(out, dict) and "error" in out:
        return JSONResponse(status_code=200, content={"success": False, "error": out["error"]})
    return JSONResponse(content={"success": True, "data": _firecrawl_scrape_data(out)})


def create_router(*, limiter: Any, optional_api_key: Callable[..., Any]) -> APIRouter:
    """Build the public web-API router. Factory so limiter (part_001) + optional_api_key
    (auth shard) inject without an import cycle — same pattern as playground.py."""
    router = APIRouter()

    @router.post("/scrape", tags=["Web"], summary="Scrape a URL to markdown / structured data (Firecrawl-shaped).")
    @limiter.limit(_WEB_API_RATE_LIMIT)
    def scrape(request: Request, body: dict[str, Any] = Body(...), caller: Any = Depends(optional_api_key)) -> JSONResponse:
        if not _web_api_enabled():
            raise _disabled_error()
        url = _require_url(body)
        formats = body.get("formats") if isinstance(body.get("formats"), list) else ["markdown"]
        return _scrape_or_error({"url": url, "goal": body.get("goal") or "", "formats": formats})

    @router.post("/extract", tags=["Web"], summary="Schema-validated structured extraction from a URL.")
    @limiter.limit(_WEB_API_RATE_LIMIT)
    def extract(request: Request, body: dict[str, Any] = Body(...), caller: Any = Depends(optional_api_key)) -> JSONResponse:
        if not _web_api_enabled():
            raise _disabled_error()
        url = _require_url(body)
        payload = {"url": url, "goal": body.get("prompt") or body.get("goal") or "extract structured data",
                   "formats": ["structured"]}
        if isinstance(body.get("schema"), dict):
            payload["schema"] = body["schema"]
        return _scrape_or_error(payload)

    @router.post("/map", tags=["Web"], summary="Discover a site's URLs (sitemap + links).")
    @limiter.limit(_WEB_API_RATE_LIMIT)
    def map_urls(request: Request, body: dict[str, Any] = Body(...), caller: Any = Depends(optional_api_key)) -> JSONResponse:
        if not _web_api_enabled():
            raise _disabled_error()
        url = _require_url(body)
        try:
            limit = max(1, min(int(body.get("limit") or 2000), 2000))
        except (TypeError, ValueError):
            limit = 2000
        result = _sitemap.map_site(url, limit=limit)
        return JSONResponse(content={"success": True, "links": result["urls"], "count": result["count"]})

    @router.post("/crawl", tags=["Web"], summary="Crawl a site (bounded BFS) to markdown per page.")
    @limiter.limit(_WEB_API_RATE_LIMIT)
    def crawl_urls(request: Request, body: dict[str, Any] = Body(...), caller: Any = Depends(optional_api_key)) -> JSONResponse:
        if not _web_api_enabled():
            raise _disabled_error()
        url = _require_url(body)
        try:
            limit = max(1, min(int(body.get("limit") or 25), _CRAWL_LIMIT_HARD))
        except (TypeError, ValueError):
            limit = 25
        result = _crawl.crawl_site(
            url, limit=limit, max_depth=int(body.get("maxDepth") or 2),
            include=body.get("includePaths"), exclude=body.get("excludePaths"),
        )
        pages = [{"markdown": p["markdown"], "metadata": {"title": p["title"], "sourceURL": p["url"]}}
                 for p in result["pages"]]
        return JSONResponse(content={
            "success": True, "status": "completed",
            "total": result["count"], "completed": result["count"], "data": pages,
        })

    @router.post("/web/verify", tags=["Web"], summary="Verify a signed observation receipt (provenance, offline).")
    def verify(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
        # Always on (no outbound, no money). Accepts a full receipt object or {receipt_id}.
        receipt_id = body.get("receipt_id")
        if isinstance(receipt_id, str) and receipt_id.strip():
            return JSONResponse(content=observation_receipts.verify_observation_receipt(receipt_id.strip()))
        receipt = body.get("receipt") if isinstance(body.get("receipt"), dict) else body
        agent_id = str(receipt.get("agent_id") or "")
        public_pem = observation_receipts._resolve_public_pem(agent_id)
        if not public_pem:
            return JSONResponse(content={"valid": False, "error": "signer_key_unavailable", "claim": "provenance_only"})
        # Pass the agent's REAL did so a forged signer_did in the receipt can't pass.
        expected_did = observation_receipts._resolve_signer_did(agent_id)
        return JSONResponse(
            content=observation_receipts.verify_receipt_object(receipt, public_pem, expected_did=expected_did)
        )

    return router
