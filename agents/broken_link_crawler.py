"""
broken_link_crawler.py — Crawl a site, report broken links + mixed content.

Input:
  {
    "url": "https://example.com",      # required, the seed URL
    "max_pages": 25,                    # optional, default 25, hard cap 50
    "max_depth": 2,                     # optional, default 2, hard cap 4
    "include_external": false,          # if true, HEAD-checks first-hop external links too
    "check_images": true                # if true, audits <img alt=""> presence
  }

Output:
  {
    "seed_url": str,
    "origin": str,
    "pages_crawled": int,
    "links_checked": int,
    "broken_links": [
      {"url": str, "status_code": int | null, "found_on": str, "reason": str}
    ],
    "redirect_chains": [
      {"url": str, "found_on": str, "final_url": str, "hops": int}
    ],
    "mixed_content": [
      {"page_url": str, "asset_url": str}
    ],
    "missing_alt_text": [
      {"page_url": str, "img_src": str}
    ],
    "summary": {
      "broken_count": int,
      "redirects_count": int,
      "mixed_content_count": int,
      "missing_alt_count": int
    },
    "billing_units_actual": int   # = pages_crawled (variable pricing)
  }

Notes:
  - Same-origin BFS only by default. External links are HEAD-checked one hop deep
    when include_external=true.
  - Every URL passes core.url_security.validate_outbound_url before any I/O.
  - Hard timeout per request (8s), hard wall-clock cap (60s).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from core.url_security import validate_outbound_url
from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

_DEFAULT_MAX_PAGES = 25
_HARD_MAX_PAGES = 50
_DEFAULT_MAX_DEPTH = 2
_HARD_MAX_DEPTH = 4
_REQUEST_TIMEOUT_S = 8.0
_WALL_CLOCK_BUDGET_S = 60.0
_CONCURRENCY = 6
_MAX_BROKEN_TO_REPORT = 100
_MAX_REDIRECT_HOPS = 6
_MAX_HTML_BYTES = 1_500_000
_USER_AGENT = "Aztea-Broken-Link-Crawler/1.0"
_REPORT_LIST_CAP = 50
_HEAD_RETRY_GET_STATUSES = frozenset({405, 400, 501})



def _normalize(url: str) -> str:
    """Drop fragments and trailing whitespace; preserve query."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip()
    return urlunparse(parsed._replace(fragment=""))


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def _is_html(content_type: str | None) -> bool:
    if not content_type:
        return False
    ct = content_type.split(";", 1)[0].strip().lower()
    return ct in {"text/html", "application/xhtml+xml"}


async def _fetch_page(
    client: httpx.AsyncClient, url: str
) -> tuple[int | None, str | None, bytes | None, str | None]:
    """Returns (status_code, content_type, body_bytes_or_None, error_msg)."""
    try:
        async with client.stream("GET", url) as resp:
            content_type = resp.headers.get("content-type")
            body: bytes | None = None
            if _is_html(content_type):
                # Stream-cap to avoid pulling huge HTML into memory.
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= _MAX_HTML_BYTES:
                        break
                body = b"".join(chunks)
            else:
                # Drain so the connection releases.
                async for _ in resp.aiter_bytes():
                    pass
            return int(resp.status_code), content_type, body, None
    except httpx.HTTPError as exc:
        return None, None, None, f"{type(exc).__name__}: {exc}"


async def _check_link(
    client: httpx.AsyncClient, url: str
) -> tuple[int | None, str | None, str | None]:
    """Side-effect: HEAD then GET-fallback. Returns ``(status, final_url, error_msg)``.

    Why: a handful of servers respond 405/501 to HEAD, so we fall through
    to a 1-byte GET to distinguish ``link works`` from ``HEAD-disabled``.
    """
    try:
        resp = await client.head(url)
        if resp.status_code in _HEAD_RETRY_GET_STATUSES:
            resp = await client.get(url, headers={"Range": "bytes=0-0"})
        return int(resp.status_code), str(resp.url), None
    except httpx.HTTPError as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def _extract_links_and_assets(
    page_url: str, html: bytes
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Returns (link_urls, asset_urls, missing_alt_imgs)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        _LOG.warning("BeautifulSoup parse failed for %s", page_url, exc_info=True)
        return [], [], []

    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = str(tag.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        links.append(_normalize(absolute))

    assets: list[str] = []
    for tag_name, attr in (("img", "src"), ("script", "src"), ("link", "href")):
        for tag in soup.find_all(tag_name, **{attr: True}):
            asset = str(tag.get(attr) or "").strip()
            if not asset or asset.startswith("data:"):
                continue
            assets.append(_normalize(urljoin(page_url, asset)))

    missing_alt: list[dict[str, str]] = []
    for img in soup.find_all("img"):
        alt = img.get("alt")
        if alt is None or str(alt).strip() == "":
            src = str(img.get("src") or "").strip()
            if src:
                missing_alt.append({"page_url": page_url, "img_src": _normalize(urljoin(page_url, src))})

    return links, assets, missing_alt


def _record_broken(
    broken: list[dict[str, Any]], *, url: str, status: int | None,
    found_on: str, reason: str,
) -> None:
    """Side-effect (mutating ``broken``): append a broken-link record under the report cap."""
    if len(broken) >= _MAX_BROKEN_TO_REPORT:
        return
    broken.append({
        "url": url, "status_code": status, "found_on": found_on, "reason": reason,
    })


def _candidate_link_targets(
    page_links: list[str], seed_url: str, *, include_external: bool,
    links_checked: set[str],
) -> list[str]:
    """Pure-ish (mutates ``links_checked``): pick the next-hop links worth HEAD-checking."""
    targets: list[str] = []
    for link in page_links:
        if link in links_checked:
            continue
        if not _same_origin(link, seed_url) and not include_external:
            continue
        try:
            validate_outbound_url(link, "link")
        except ValueError:
            continue
        links_checked.add(link)
        targets.append(link)
    return targets


async def _check_link_targets(
    client: httpx.AsyncClient, targets: list[str], source_url: str, *,
    deadline: float, broken: list[dict[str, Any]],
    redirects: list[dict[str, Any]],
) -> None:
    """Side-effect: HEAD-check ``targets`` concurrently and append outcomes to ``broken``/``redirects``."""
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _check_one(target: str) -> None:
        async with sem:
            if time.monotonic() > deadline:
                return
            status_code, final_url, err_msg = await _check_link(client, target)
            if status_code is None:
                _record_broken(broken, url=target, status=None, found_on=source_url,
                               reason=err_msg or "fetch failed")
                return
            if status_code >= 400:
                _record_broken(broken, url=target, status=status_code,
                               found_on=source_url, reason=f"HTTP {status_code}")
            elif final_url and _normalize(final_url) != target:
                redirects.append({
                    "url": target, "found_on": source_url,
                    "final_url": final_url, "hops": 1,
                })

    if targets:
        await asyncio.gather(*(_check_one(t) for t in targets))


def _record_page_status(
    *, broken: list[dict[str, Any]], url: str, depth: int,
    status: int | None, err: str | None,
) -> None:
    """Side-effect: mutate ``broken`` if a fetched page itself failed or returned 4xx/5xx."""
    found_on = "(seed)" if depth == 0 else "(crawl)"
    if status is None:
        _record_broken(broken, url=url, status=None, found_on=found_on,
                       reason=err or "fetch failed")
    elif status >= 400:
        _record_broken(broken, url=url, status=status, found_on=found_on,
                       reason=f"HTTP {status}")


async def _process_page(
    client: httpx.AsyncClient, url: str, depth: int, *,
    seed_url: str, seed_is_https: bool, max_depth: int, include_external: bool,
    check_images: bool, queue: list[tuple[str, int]], visited_pages: set[str],
    links_checked: set[str], broken: list[dict[str, Any]],
    redirects: list[dict[str, Any]], mixed: list[dict[str, str]],
    missing_alt: list[dict[str, str]], deadline: float,
) -> None:
    """Side-effect: fetch one page and update every result accumulator in place."""
    status, ctype, body, err = await _fetch_page(client, url)
    _record_page_status(broken=broken, url=url, depth=depth, status=status, err=err)
    if not body or not _is_html(ctype):
        return
    page_links, page_assets, page_missing_alt = _extract_links_and_assets(url, body)
    if check_images:
        missing_alt.extend(page_missing_alt)
    if seed_is_https:
        for asset in page_assets:
            if asset.lower().startswith("http://"):
                mixed.append({"page_url": url, "asset_url": asset})
    if depth < max_depth:
        for child in page_links:
            if _same_origin(child, seed_url) and child not in visited_pages:
                queue.append((child, depth + 1))
    targets = _candidate_link_targets(
        page_links, seed_url, include_external=include_external, links_checked=links_checked,
    )
    await _check_link_targets(
        client, targets, url, deadline=deadline, broken=broken, redirects=redirects,
    )


def _build_crawl_result(
    *, seed_url: str, origin: str, pages_crawled: int, links_checked: int,
    broken: list[dict[str, Any]], redirects: list[dict[str, Any]],
    mixed: list[dict[str, str]], missing_alt: list[dict[str, str]],
) -> dict[str, Any]:
    """Pure: shape accumulator state into the public crawl response envelope."""
    return {
        "seed_url": seed_url,
        "origin": origin,
        "pages_crawled": pages_crawled,
        "links_checked": links_checked,
        "broken_links": broken,
        "redirect_chains": redirects[:_REPORT_LIST_CAP],
        "mixed_content": mixed[:_REPORT_LIST_CAP],
        "missing_alt_text": missing_alt[:_REPORT_LIST_CAP],
        "summary": {
            "broken_count": len(broken),
            "redirects_count": len(redirects),
            "mixed_content_count": len(mixed),
            "missing_alt_count": len(missing_alt),
        },
        "billing_units_actual": max(1, pages_crawled),
    }


def _new_crawl_state(seed_url: str) -> dict[str, Any]:
    """Pure: fresh accumulator state for a crawl run."""
    return {
        "origin": f"{urlparse(seed_url).scheme}://{urlparse(seed_url).hostname}",
        "queue": [(seed_url, 0)],
        "visited_pages": set(),
        "links_checked": set(),
        "broken": [],
        "redirects": [],
        "mixed": [],
        "missing_alt": [],
        "pages_crawled": 0,
        "seed_is_https": seed_url.lower().startswith("https://"),
        "deadline": time.monotonic() + _WALL_CLOCK_BUDGET_S,
    }


async def _crawl_loop(
    client: httpx.AsyncClient, state: dict[str, Any], *,
    seed_url: str, max_pages: int, max_depth: int,
    include_external: bool, check_images: bool,
) -> None:
    """Side-effect: BFS over the page queue, mutating ``state`` until budget or queue exhausts."""
    while state["queue"] and state["pages_crawled"] < max_pages:
        if time.monotonic() > state["deadline"]:
            break
        url, depth = state["queue"].pop(0)
        url = _normalize(url)
        if url in state["visited_pages"]:
            continue
        state["visited_pages"].add(url)
        state["pages_crawled"] += 1
        await _process_page(
            client, url, depth,
            seed_url=seed_url, seed_is_https=state["seed_is_https"], max_depth=max_depth,
            include_external=include_external, check_images=check_images,
            queue=state["queue"], visited_pages=state["visited_pages"],
            links_checked=state["links_checked"],
            broken=state["broken"], redirects=state["redirects"],
            mixed=state["mixed"], missing_alt=state["missing_alt"],
            deadline=state["deadline"],
        )


async def _crawl(
    seed_url: str,
    max_pages: int,
    max_depth: int,
    include_external: bool,
    check_images: bool,
) -> dict[str, Any]:
    """Side-effect: same-origin BFS crawl bounded by ``max_pages``, ``max_depth`` and wall-clock budget."""
    state = _new_crawl_state(seed_url)
    limits = httpx.Limits(max_connections=_CONCURRENCY, max_keepalive_connections=_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=_REQUEST_TIMEOUT_S,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
        limits=limits,
    ) as client:
        await _crawl_loop(
            client, state,
            seed_url=seed_url, max_pages=max_pages, max_depth=max_depth,
            include_external=include_external, check_images=check_images,
        )
    return _build_crawl_result(
        seed_url=seed_url, origin=state["origin"],
        pages_crawled=state["pages_crawled"], links_checked=len(state["links_checked"]),
        broken=state["broken"], redirects=state["redirects"],
        mixed=state["mixed"], missing_alt=state["missing_alt"],
    )


def _normalize_run_inputs(payload: dict) -> dict | tuple[str, int, int, bool, bool]:
    """Pure: validate inputs; returns parsed bag or an error envelope."""
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("broken_link_crawler.missing_url", "url is required")
    try:
        seed = validate_outbound_url(raw_url, "url")
    except ValueError as exc:
        return _err("broken_link_crawler.invalid_url", str(exc))
    try:
        max_pages = int(payload.get("max_pages") or _DEFAULT_MAX_PAGES)
    except (TypeError, ValueError):
        max_pages = _DEFAULT_MAX_PAGES
    max_pages = max(1, min(max_pages, _HARD_MAX_PAGES))
    try:
        max_depth = int(payload.get("max_depth") or _DEFAULT_MAX_DEPTH)
    except (TypeError, ValueError):
        max_depth = _DEFAULT_MAX_DEPTH
    max_depth = max(0, min(max_depth, _HARD_MAX_DEPTH))
    include_external = bool(payload.get("include_external", False))
    check_images = bool(payload.get("check_images", True))
    return seed, max_pages, max_depth, include_external, check_images


def _run_crawl_sync(
    seed: str, max_pages: int, max_depth: int, include_external: bool, check_images: bool,
) -> dict[str, Any]:
    """Side-effect: drive the async crawler from a sync caller.

    Why: agent ``run`` functions are sync per contract; if we're already inside
    an event loop the defensive branch creates a fresh loop instead of asyncio
    asserting "asyncio.run() cannot be called from a running event loop".
    """
    try:
        return asyncio.run(_crawl(seed, max_pages, max_depth, include_external, check_images))
    except RuntimeError as exc:
        if "asyncio.run() cannot be called" not in str(exc):
            return _err("broken_link_crawler.runtime_failed", str(exc))
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _crawl(seed, max_pages, max_depth, include_external, check_images)
            )
        finally:
            loop.close()


def run(payload: dict) -> dict:
    """Crawl a site and report broken links, redirect chains, mixed content, and missing alt text.

    Why: same-origin BFS by default with bounded concurrency keeps the call
    deterministic; HEAD-only link checks minimise bandwidth on large sites.
    """
    parsed = _normalize_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed
    seed, max_pages, max_depth, include_external, check_images = parsed
    return _run_crawl_sync(seed, max_pages, max_depth, include_external, check_images)
