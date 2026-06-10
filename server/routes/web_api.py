"""Public, Firecrawl-shaped web API (Phase D) + the public verify endpoint (Phase F).

# OWNS: the developer-facing HTTP surface — POST /scrape /map /crawl /extract (one
#        engine, Firecrawl-compatible response shape), POST /web/verify (offline
#        receipt verification, the provenance differentiator), and POST /web/act (the
#        web_actor test surface: interact / dry_run / preview, gated separately).
# NOT OWNS: the engine (agents.site_navigator, core.web.sitemap/crawl), SSRF policy
#           (the engine validates every URL), or receipt signing (core.observation_receipts).
# INVARIANTS:
#   * /scrape /map /crawl /extract are gated by AZTEA_WEB_API_ENABLED (default OFF) —
#     they make outbound fetches, an abuse surface, so the off path is the default.
#   * Anonymous-callable + IP-rate-limited (the zero-auth demo tier, review fix #12).
#   * Every outbound URL is SSRF-validated inside the engine — the routes add nothing
#     that bypasses it.
#   * /web/verify is ALWAYS on (no outbound, no money) — it only checks a signature.
#   * /web/act drives a real browser, so it is gated by AZTEA_ACTION_WEB_ENABLED (the
#     web_actor master switch), NOT AZTEA_WEB_API_ENABLED — off by default.
# DECISIONS:
#   * Factory pattern matches server/routes/playground.py + public_integrations.py so the
#     part_*.py shards stay under the line budget.
#   * Response shape is Firecrawl-compatible ({success, data:{markdown,html,links,json,
#     metadata}}) so a developer can point a Firecrawl client at this base URL (review T2).
"""

from __future__ import annotations

import concurrent.futures as _cf
import logging
import os
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from urllib.parse import urlparse

from agents import _html_extract, _site_fetch
from agents import site_navigator as _site_navigator
from agents import web_actor as _web_actor
from core import action_mandates as _action_mandates
from core import error_codes, feature_flags, observation_receipts
from core.web import crawl as _crawl
from core.web import sitemap as _sitemap
from server.builtin_agents.constants import WEB_ACTOR_AGENT_ID as _WEB_ACTOR_AGENT_ID

_LOG = logging.getLogger(__name__)

_WEB_API_RATE_LIMIT = "10/minute"
_MAX_URL_CHARS = 2_000
_WEB_ACT_DEFAULT_CAP_USD = 1.0  # throwaway-mandate cap for the dry_run test surface
# Public crawl is tightly bounded (worker-pool protection); crawl_site also enforces a
# total wall-clock budget so one call can't pin a worker.
_CRAWL_LIMIT_HARD = 25


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
            # Real status when the serving path knew it (http_first); the rendered
            # path reached a successful capture, so 200 is the honest default there.
            "statusCode": nav_out.get("http_status") or 200,
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


_FOLLOW_LINKS_MAX = 30            # hard cap on linked pages followed per scrape (worker safety)
_FOLLOW_CONCURRENCY = 6           # parallel link fetches so "read all stories" isn't slow
_LINKED_PAGE_MARKDOWN_MAX = 20_000  # per-linked-page markdown cap
# Below this much markdown a followed article is "thin" (mirrors the playground's
# little-text flag) and is a candidate for the bounded render-fallback pass.
_LINKED_PAGE_THIN_CHARS = 400
# Render fallback is a full Chromium launch per page, so it is tightly bounded: only
# the first N thin/failed links get a render, the rest stay honest-but-thin.
_FOLLOW_RENDER_MAX = 3


def _follow_links_count(raw: Any) -> int:
    """Pure: clamp the requested follow-links count to [0, _FOLLOW_LINKS_MAX]."""
    try:
        return max(0, min(int(raw), _FOLLOW_LINKS_MAX))
    except (TypeError, ValueError):
        return 0


def _fetch_one_linked(link: dict[str, str]) -> dict[str, Any]:
    """Fetch + clean-markdown one content link (HTTP-first). SSRF is validated inside
    fetch_static_html. A dead/blocked/JS-only link returns an EMPTY entry rather than
    being dropped — silently losing a story the caller asked for misreads as "the feed
    only had N articles". needs_render marks it for the bounded Chromium pass."""
    try:
        fetched = _site_fetch.fetch_static_html(link["url"])
    except Exception:  # noqa: BLE001 — surfaced as an empty entry, logged
        _LOG.debug("follow-link fetch failed: %s", link["url"], exc_info=True)
        fetched = None
    if fetched is None:
        return {"url": link["url"], "title": link["text"], "markdown": "", "chars": 0,
                "via": "http", "needs_render": True}
    # Strip images (we want the article TEXT, not broken-icon embeds) and report the
    # readable-text length so the UI can lead with a substantial article, not an empty one.
    markdown = _html_extract.strip_images(_html_extract.to_markdown(fetched.html))[:_LINKED_PAGE_MARKDOWN_MAX]
    thin = len(markdown) < _LINKED_PAGE_THIN_CHARS
    return {
        "url": link["url"],
        "title": _html_extract.title_of(fetched.html) or link["text"],
        "markdown": markdown,
        "chars": len(markdown),
        "via": "http",
        "needs_render": thin and _html_extract.analyze_html(fetched.html).needs_browser,
    }


def _render_one_linked(page: dict[str, Any]) -> dict[str, Any]:
    """Mutates-and-returns: re-fetch one thin/failed linked page through the navigator
    (its own http-first -> Chromium ladder) and keep whichever markdown is longer.
    Best-effort — a render failure leaves the honest thin entry in place."""
    try:
        out = _site_navigator.run({"url": page["url"], "formats": ["markdown"]})
    except Exception:  # noqa: BLE001 — fallback is additive; the thin entry stands, logged
        _LOG.warning("follow-link render fallback failed: %s", page["url"], exc_info=True)
        return page
    rendered = _html_extract.strip_images(str(out.get("markdown") or ""))[:_LINKED_PAGE_MARKDOWN_MAX]
    if len(rendered) > page["chars"]:
        title = str((out.get("site_map") or {}).get("title") or "")
        page.update({"markdown": rendered, "chars": len(rendered), "via": "render",
                     "title": title or page["title"]})
    return page


def _expand_linked_pages(base_url: str, html: str, limit: int) -> list[dict[str, Any]]:
    """Follow the page's CONTENT links (up to `limit`) IN PARALLEL and return each linked
    page's clean markdown — so a feed/index scrape returns the actual articles, not just
    the headline list. Document order is preserved. Links that fetched dead/thin get a
    second chance through the bounded Chromium fallback (first _FOLLOW_RENDER_MAX only —
    renders are expensive); whatever is still thin is returned honestly with chars: 0-ish."""
    links = _html_extract.content_links(html, limit=limit, base_url=base_url)
    if not links:
        return []
    with _cf.ThreadPoolExecutor(max_workers=min(_FOLLOW_CONCURRENCY, len(links))) as pool:
        pages = list(pool.map(_fetch_one_linked, links))  # order-preserving
    retry = [p for p in pages if p.pop("needs_render", False)][:_FOLLOW_RENDER_MAX]
    if retry:
        with _cf.ThreadPoolExecutor(max_workers=min(_FOLLOW_RENDER_MAX, len(retry))) as pool:
            list(pool.map(_render_one_linked, retry))  # in-place updates, order kept via `pages`
    return pages


def _web_act_cap_cents(raw: Any) -> int:
    """Pure: convert a caller-entered USD cap to integer cents for the throwaway test
    mandate (default $1). create_mandate re-clamps to the platform ceiling, so this is
    only the test-surface convenience, never an authority on spend."""
    try:
        usd = float(raw) if raw is not None else _WEB_ACT_DEFAULT_CAP_USD
    except (TypeError, ValueError):
        usd = _WEB_ACT_DEFAULT_CAP_USD
    return max(0, int(round(usd * 100)))


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
        formats = list(body["formats"]) if isinstance(body.get("formats"), list) else ["markdown"]
        follow = _follow_links_count(body.get("follow_links"))
        # Need the page HTML to find content links; fetch it internally when following,
        # then drop it from the response if the caller didn't ask for it.
        internal = formats if (not follow or "html" in formats) else [*formats, "html"]
        out = _site_navigator.run({"url": url, "goal": body.get("goal") or "", "formats": internal})
        if isinstance(out, dict) and "error" in out:
            return JSONResponse(status_code=200, content={"success": False, "error": out["error"]})
        data = _firecrawl_scrape_data(out)
        if follow:
            data["linked_pages"] = _expand_linked_pages(out.get("url") or url, out.get("html") or "", follow)
            if "html" not in formats:
                data.pop("html", None)
        return JSONResponse(content={"success": True, "data": data})

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

    @router.post("/web/act", tags=["Web"], summary="Test surface for web_actor (interact / dry_run / preview).")
    @limiter.limit(_WEB_API_RATE_LIMIT)
    def web_act(request: Request, body: dict[str, Any] = Body(...), caller: Any = Depends(optional_api_key)) -> JSONResponse:
        # Gated like web_actor itself: this drives a real browser, so it is OFF until an
        # operator opts in. dry_run/preview need a mandate, so we mint a throwaway one
        # bound to the target host (the agent's own gates still apply downstream).
        if not feature_flags.action_web_enabled():
            raise HTTPException(
                status_code=503,
                detail=error_codes.make_error(
                    "web_actor.disabled",
                    "The write-web is disabled. Set AZTEA_ACTION_WEB_ENABLED=1 to test web_actor.",
                ),
            )
        url = _require_url(body)
        action = str(body.get("action") or "interact").strip().lower()
        steps = body.get("steps") if isinstance(body.get("steps"), list) else []
        if action == "interact":
            return JSONResponse(content=_web_actor.run({"action": "interact", "url": url, "steps": steps}))
        owner = str((caller or {}).get("owner_id") or "web_act_playground")
        mandate = _action_mandates.create_mandate(
            caller_owner_id=owner, agent_id=_WEB_ACTOR_AGENT_ID, action_kind="purchase",
            reversibility="unknown", max_spend_cents=_web_act_cap_cents(body.get("max_spend_usd")),
            allowed_domains=[(urlparse(url).hostname or "").lower()],
            action_descriptor={"source": "web_act_playground"},
        )
        payload: dict[str, Any] = {"action": action, "url": url,
                                   "mandate_id": mandate["mandate_id"], "steps": steps}
        if body.get("use_credential"):
            payload["use_credential"] = str(body["use_credential"]).strip().lower()
        return JSONResponse(content=_web_actor.run(payload))

    return router
