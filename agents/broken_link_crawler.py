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
import time
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from core.url_security import validate_outbound_url

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


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


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
    """HEAD then GET-fallback. Returns (status_code, final_url, error_msg)."""
    try:
        resp = await client.head(url)
        # Some servers respond 405/501 to HEAD — fall back to a tiny GET.
        if resp.status_code in {405, 400, 501}:
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


async def _crawl(
    seed_url: str,
    max_pages: int,
    max_depth: int,
    include_external: bool,
    check_images: bool,
) -> dict[str, Any]:
    origin = f"{urlparse(seed_url).scheme}://{urlparse(seed_url).hostname}"

    visited_pages: set[str] = set()
    queue: list[tuple[str, int]] = [(seed_url, 0)]
    pages_crawled = 0
    links_checked: set[str] = set()
    broken_links: list[dict[str, Any]] = []
    redirect_chains: list[dict[str, Any]] = []
    mixed_content: list[dict[str, str]] = []
    missing_alt_text: list[dict[str, str]] = []

    seed_is_https = seed_url.lower().startswith("https://")
    deadline = time.monotonic() + _WALL_CLOCK_BUDGET_S

    limits = httpx.Limits(max_connections=_CONCURRENCY, max_keepalive_connections=_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=_REQUEST_TIMEOUT_S,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
        limits=limits,
    ) as client:
        while queue and pages_crawled < max_pages:
            if time.monotonic() > deadline:
                break

            url, depth = queue.pop(0)
            url = _normalize(url)
            if url in visited_pages:
                continue
            visited_pages.add(url)

            status, ctype, body, err = await _fetch_page(client, url)
            pages_crawled += 1
            if status is None:
                broken_links.append(
                    {
                        "url": url,
                        "status_code": None,
                        "found_on": "(seed)" if depth == 0 else "(crawl)",
                        "reason": err or "fetch failed",
                    }
                )
                continue
            if status >= 400:
                broken_links.append(
                    {
                        "url": url,
                        "status_code": status,
                        "found_on": "(seed)" if depth == 0 else "(crawl)",
                        "reason": f"HTTP {status}",
                    }
                )

            if not body or not _is_html(ctype):
                continue

            page_links, page_assets, page_missing_alt = _extract_links_and_assets(url, body)
            if check_images:
                missing_alt_text.extend(page_missing_alt)

            # Mixed content: HTTPS page loading HTTP assets.
            if seed_is_https:
                for asset in page_assets:
                    if asset.lower().startswith("http://"):
                        mixed_content.append({"page_url": url, "asset_url": asset})

            # Enqueue same-origin children for BFS expansion.
            if depth < max_depth:
                for child in page_links:
                    if _same_origin(child, seed_url) and child not in visited_pages:
                        queue.append((child, depth + 1))

            # HEAD-check candidate links (same-origin + optionally external first-hop).
            link_targets: list[str] = []
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
                link_targets.append(link)

            # Bound concurrency on the link checks.
            sem = asyncio.Semaphore(_CONCURRENCY)

            async def _check_one(target: str) -> None:
                async with sem:
                    if time.monotonic() > deadline:
                        return
                    status_code, final_url, err_msg = await _check_link(client, target)
                    if status_code is None:
                        if len(broken_links) < _MAX_BROKEN_TO_REPORT:
                            broken_links.append(
                                {
                                    "url": target,
                                    "status_code": None,
                                    "found_on": url,
                                    "reason": err_msg or "fetch failed",
                                }
                            )
                        return
                    if status_code >= 400:
                        if len(broken_links) < _MAX_BROKEN_TO_REPORT:
                            broken_links.append(
                                {
                                    "url": target,
                                    "status_code": status_code,
                                    "found_on": url,
                                    "reason": f"HTTP {status_code}",
                                }
                            )
                    elif final_url and _normalize(final_url) != target:
                        redirect_chains.append(
                            {
                                "url": target,
                                "found_on": url,
                                "final_url": final_url,
                                "hops": 1,
                            }
                        )

            if link_targets:
                await asyncio.gather(*(_check_one(t) for t in link_targets))

    return {
        "seed_url": seed_url,
        "origin": origin,
        "pages_crawled": pages_crawled,
        "links_checked": len(links_checked),
        "broken_links": broken_links,
        "redirect_chains": redirect_chains[:50],
        "mixed_content": mixed_content[:50],
        "missing_alt_text": missing_alt_text[:50],
        "summary": {
            "broken_count": len(broken_links),
            "redirects_count": len(redirect_chains),
            "mixed_content_count": len(mixed_content),
            "missing_alt_count": len(missing_alt_text),
        },
        "billing_units_actual": max(1, pages_crawled),
    }


def run(payload: dict) -> dict:
    """Crawl a website and report broken links, redirect chains, mixed content,
    and images missing alt text. Same-origin BFS by default; bounded concurrency.

    Variable pricing: billed per page actually crawled (see specs).
    """
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

    try:
        return asyncio.run(
            _crawl(seed, max_pages, max_depth, include_external, check_images)
        )
    except RuntimeError as exc:
        # If we're already inside an event loop (rare in our sync invocation
        # path, but defensive), fall back to a fresh loop.
        if "asyncio.run() cannot be called" in str(exc):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    _crawl(seed, max_pages, max_depth, include_external, check_images)
                )
            finally:
                loop.close()
        return _err("broken_link_crawler.runtime_failed", str(exc))
