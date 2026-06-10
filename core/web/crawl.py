"""Bounded multi-page crawl (/crawl): BFS that fetches each page to clean markdown.

# OWNS: same-domain breadth-first crawl with depth/limit/path-filter bounds, returning
#        markdown + title per page.
# NOT OWNS: the fetch (agents._site_fetch, SSRF + IP-pin), markdown/link extraction
#           (agents._html_extract), the registrable-domain gate (api_discovery).
# INVARIANTS:
#   * Every page goes through _site_fetch (SSRF + IP-pinning apply to every hop).
#   * Hard-bounded: never more than ``limit`` pages, never deeper than ``max_depth``,
#     only same-registrable-domain links are enqueued — a crawl can't run away.
# DECISIONS:
#   * v1 crawls the STATIC HTML of each page (no Chromium): cheap and parallel-friendly.
#     JS-only content is out of scope for bulk crawl; the goal-directed navigator (with
#     its Chromium fallback) is the path for a single JS-heavy page.
"""

from __future__ import annotations

import fnmatch
import logging
import time
from collections import deque
from urllib.parse import urljoin, urlparse

from agents import _html_extract, _site_fetch
from core.site_maps import api_discovery as _ad

_LOG = logging.getLogger(__name__)

_CRAWL_LIMIT_DEFAULT = 50
_CRAWL_LIMIT_MAX = 200
# Total wall-clock budget for one crawl. Each page fetch can block up to the
# http-first timeout, and prod runs a small worker pool, so a deep/slow site must
# not pin a worker for minutes — the BFS stops once this elapses.
_CRAWL_WALL_CLOCK_BUDGET_S = 25.0
_CRAWL_MAX_DEPTH = 3
_PAGE_MARKDOWN_CAP = 100_000
_LINKS_PER_PAGE = 50


def _dedup_key(url: str) -> str:
    """Pure: the identity of a URL for crawl dedup — fragment stripped, trailing slash
    folded. ``https://x.com`` and ``https://x.com/`` are the same resource; without
    this a feed that links both forms burns two fetch slots on identical markdown."""
    return url.split("#")[0].rstrip("/")


def _path_allowed(url: str, include: list[str] | None, exclude: list[str] | None) -> bool:
    """Pure: path-glob gate. exclude wins; an include list (if given) must match."""
    path = urlparse(url).path or "/"
    if exclude and any(fnmatch.fnmatch(path, pat) for pat in exclude):
        return False
    if include and not any(fnmatch.fnmatch(path, pat) for pat in include):
        return False
    return True


def crawl_site(
    start_url: str, *, limit: int = _CRAWL_LIMIT_DEFAULT, max_depth: int = 2,
    include: list[str] | None = None, exclude: list[str] | None = None,
) -> dict:
    """Bounded BFS crawl. Returns {start, pages:[{url,title,markdown}], count, limit_reached}.

    Follows only same-registrable-domain links, honors include/exclude path globs, and
    stops at ``limit`` pages or ``max_depth`` hops (both hard-capped).
    """
    capped_limit = max(1, min(int(limit), _CRAWL_LIMIT_MAX))
    capped_depth = max(0, min(int(max_depth), _CRAWL_MAX_DEPTH))
    host = urlparse(start_url).hostname or ""
    seen: set[str] = set()
    pages: list[dict] = []
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    started = time.monotonic()

    while queue and len(pages) < capped_limit and (time.monotonic() - started) < _CRAWL_WALL_CLOCK_BUDGET_S:
        url, depth = queue.popleft()
        norm = url.split("#")[0]
        key = _dedup_key(norm)
        if key in seen:
            continue
        seen.add(key)
        if not _path_allowed(norm, include, exclude):
            continue
        fetched = _site_fetch.fetch_static_html(norm)
        if fetched is None:
            continue
        # Redirects converge: two queued URLs can land on one final page. Dedup the
        # landing URL too, or the page is emitted twice with identical markdown.
        final_key = _dedup_key(fetched.final_url)
        if final_key != key:
            if final_key in seen:
                continue
            seen.add(final_key)
        pages.append({
            "url": fetched.final_url,
            "title": _html_extract.title_of(fetched.html),
            "markdown": _html_extract.to_markdown(fetched.html)[:_PAGE_MARKDOWN_CAP],
        })
        if depth < capped_depth:
            _enqueue_links(fetched, host=host, seen=seen, queue=queue, depth=depth)

    return {
        "start": start_url,
        "pages": pages,
        "count": len(pages),
        "limit_reached": len(pages) >= capped_limit,
        "budget_exceeded": bool(queue) and len(pages) < capped_limit,
    }


def _enqueue_links(
    fetched: _site_fetch.FetchResult, *, host: str, seen: set[str],
    queue: "deque[tuple[str, int]]", depth: int,
) -> None:
    """Side-effect: enqueue same-domain links from a fetched page for the next depth."""
    for href in _html_extract.extract_links(fetched.html, limit=_LINKS_PER_PAGE):
        nxt = urljoin(fetched.final_url, href).split("#")[0]
        parsed = urlparse(nxt)
        if (
            parsed.scheme in ("http", "https")
            and _ad.same_registrable_domain(parsed.hostname or "", host)
            and _dedup_key(nxt) not in seen
        ):
            queue.append((nxt, depth + 1))
