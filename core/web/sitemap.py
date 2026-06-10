"""URL discovery for a site (/map): robots.txt sitemaps + sitemap.xml + page links.

# OWNS: fast same-domain URL enumeration for a site without scraping each page.
# NOT OWNS: the fetch (agents._site_fetch, SSRF + IP-pin) or link parsing
#           (agents._html_extract); the registrable-domain gate (core.site_maps.api_discovery).
# INVARIANTS:
#   * Every fetched URL goes through _site_fetch (so SSRF + IP-pinning apply).
#   * Output is capped (_MAP_URL_CAP) and nested sitemap fan-out is bounded
#     (_MAX_CHILD_SITEMAPS / depth) so a hostile sitemap index can't blow up work.
#   * Discovered URLs are filtered to the start URL's registrable domain.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from agents import _html_extract, _site_fetch
from core.site_maps import api_discovery as _ad

_LOG = logging.getLogger(__name__)

_MAP_URL_CAP = 2000
_MAX_CHILD_SITEMAPS = 10      # bound nested-sitemap-index fan-out
_MAX_SITEMAP_DEPTH = 2
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_ROBOTS_SITEMAP_RE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)\s*$")


def _origin(url: str) -> str:
    """Pure: scheme://host[:port] of a URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _robots_sitemaps(origin: str) -> list[str]:
    """Side-effect: Sitemap: lines from robots.txt (empty on miss)."""
    raw = _site_fetch.fetch_raw(urljoin(origin + "/", "robots.txt"))
    if raw is None:
        return []
    return _ROBOTS_SITEMAP_RE.findall(raw.text)[:_MAX_CHILD_SITEMAPS]


def _collect_sitemap(url: str, *, depth: int, seen: set[str], out: list[str]) -> None:
    """Side-effect: append <loc> URLs from a sitemap, recursing into indexes (bounded)."""
    if (
        url in seen or len(seen) >= _MAX_CHILD_SITEMAPS
        or len(out) >= _MAP_URL_CAP or depth > _MAX_SITEMAP_DEPTH
    ):
        return
    seen.add(url)
    raw = _site_fetch.fetch_raw(url)
    if raw is None:
        return
    is_index = "<sitemapindex" in raw.text.lower()
    for loc in _LOC_RE.findall(raw.text):
        if len(out) >= _MAP_URL_CAP:
            break
        loc = loc.strip()
        if is_index:
            _collect_sitemap(loc, depth=depth + 1, seen=seen, out=out)
        else:
            out.append(loc)


def map_site(url: str, *, limit: int = _MAP_URL_CAP) -> dict:
    """Discover same-domain URLs for a site: robots sitemaps + sitemap.xml + page links.

    Returns {site, urls, count, sources}. Best-effort: a missing robots/sitemap just
    yields fewer URLs; the page-link harvest covers sites with no sitemap at all.
    """
    capped = max(1, min(int(limit), _MAP_URL_CAP))
    origin = _origin(url)
    discovered: list[str] = []
    seen_sitemaps: set[str] = set()
    sitemap_urls = _robots_sitemaps(origin) or [urljoin(origin + "/", "sitemap.xml")]
    for sitemap_url in sitemap_urls:
        _collect_sitemap(sitemap_url, depth=0, seen=seen_sitemaps, out=discovered)
    from_sitemap = len(discovered)

    page = _site_fetch.fetch_static_html(url)
    from_links = 0
    if page is not None:
        for href in _html_extract.extract_links(page.html, limit=capped):
            discovered.append(urljoin(page.final_url, href))
            from_links += 1

    urls = _dedup_same_domain(discovered, host=urlparse(url).hostname or "", limit=capped)
    return {
        "site": origin,
        "urls": urls,
        "count": len(urls),
        "sources": {"sitemap": from_sitemap, "page_links": from_links},
    }


def _dedup_same_domain(urls: list[str], *, host: str, limit: int) -> list[str]:
    """Pure: http(s) + same-registrable-domain URLs, de-duplicated, capped."""
    out: list[str] = []
    seen: set[str] = set()
    for candidate in urls:
        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https"):
            continue
        if not _ad.same_registrable_domain(parsed.hostname or "", host):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
        if len(out) >= limit:
            break
    return out
