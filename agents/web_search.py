"""
web_search.py — Live web search via DuckDuckGo's HTML endpoint.

Input:
  {
    "query": "what is aztea",      # required, 1-400 chars
    "count": 10,                     # optional, default 10, hard cap 20
    "mode": "web",                   # optional; only "web" supported.
                                     # "news" returns the same web results.
    "country": "us-en",              # optional DDG region (e.g. us-en, gb-en, wt-wt).
    "freshness": "pd" | "pw" | "pm" | "py"  # optional past-day / week / month / year
  }

Output:
  {
    "query": str,
    "mode": str,
    "result_count": int,
    "results": [
      {
        "title": str,
        "url": str,
        "description": str,
        "age": str | null,        # null — DDG HTML scrape does not surface ages.
        "site_name": str | null,  # parsed from URL host
        "thumbnail_url": str | null
      }
    ],
    "billing_units_actual": int   # always 1
  }

Setup:
  No API key required. We call https://html.duckduckgo.com/html/ which is the
  same endpoint the official `duckduckgo-search` Python package uses. DDG is
  rate-limited: ~30 requests/minute per source IP before they start serving
  CAPTCHAs. The marketplace billing ($0.01/call) absorbs the operational cost
  of caching at the registry layer.

  Switched from Brave Search API (paid beyond 2k queries/month) on 2026-05-09
  so the marketplace listing has no per-call dependency on the host setting up
  a vendor account.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from core.url_security import validate_outbound_url
from agents._contracts import agent_error as _err

_DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
_TIMEOUT_S = 8.0
_DEFAULT_COUNT = 10
_HARD_MAX_COUNT = 20
_MAX_QUERY_LEN = 400
_VALID_MODES = ("web", "news")
_DEFAULT_REGION = "wt-wt"
# DDG `df` matches Brave's freshness enum 1:1; accept Brave-style and translate.
_FRESHNESS_TO_DDG_DF = {"pd": "d", "pw": "w", "pm": "m", "py": "y"}
# DDG's HTML endpoint rate-limits bare httpx UAs harder; mirror a real browser.
_DDG_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.5",
}



def _unwrap_ddg_redirect(href: str) -> str:
    """Convert DDG's `/l/?uddg=<encoded>&...` wrapper back into the destination URL.

    The HTML endpoint wraps every result link through a click-tracking redirect.
    We don't want the wrapped form in the response — callers expect a direct
    URL they can fetch or hand to another agent.
    """
    if not href:
        return ""
    href = href.strip()
    # Some links arrive without a scheme (//duckduckgo.com/...) and some with
    # https://. Both shapes route through the same /l/ wrapper.
    if "duckduckgo.com/l/" in href:
        try:
            qs = parse_qs(urlparse(href).query)
            target = qs.get("uddg", [""])[0]
            if target:
                return unquote(target)
        except (ValueError, TypeError):
            return href
    return href


def _site_name_from_url(url: str) -> str | None:
    try:
        host = urlparse(url).hostname or ""
    except (ValueError, TypeError):
        return None
    # Strip a leading "www." so "www.example.com" reads as "example.com".
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _parse_ddg_html(html: str, limit: int) -> list[dict[str, Any]]:
    """Extract results from html.duckduckgo.com's response.

    The DDG HTML markup uses `div.result` rows with `a.result__a` for the
    title/link and `a.result__snippet` (or `.result__snippet`) for the
    description. The structure has been stable for years; if DDG ever changes
    it the agent fails closed via the structured-error path below.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for row in soup.select("div.result, div.web-result"):
        link = row.select_one("a.result__a")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = _unwrap_ddg_redirect(str(link.get("href") or ""))
        if not title or not href:
            continue
        snippet_el = row.select_one("a.result__snippet, .result__snippet")
        description = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        out.append({
            "title": title,
            "url": href,
            "description": description,
            "age": None,
            "site_name": _site_name_from_url(href),
            "thumbnail_url": None,
        })
        if len(out) >= limit:
            break
    return out


def _validate_run_inputs(payload: dict) -> dict | tuple[str, int, str, str, str | None]:
    """Pure: validate ``query``/``count``/``mode``/``country``/``freshness``; returns parsed bag or error envelope."""
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    query = str(payload.get("query") or "").strip()
    if not query:
        return _err("web_search.missing_query", "query is required.")
    if len(query) > _MAX_QUERY_LEN:
        return _err(
            "web_search.query_too_long",
            f"query exceeds {_MAX_QUERY_LEN} characters.",
        )
    try:
        count = int(payload.get("count") or _DEFAULT_COUNT)
    except (TypeError, ValueError):
        return _err("web_search.invalid_count", "count must be an integer.")
    count = max(1, min(count, _HARD_MAX_COUNT))
    mode = str(payload.get("mode") or "web").strip().lower()
    if mode not in _VALID_MODES:
        return _err(
            "web_search.invalid_mode",
            f"mode must be one of: {', '.join(_VALID_MODES)}.",
        )
    region = str(payload.get("country") or _DEFAULT_REGION).strip().lower()
    if len(region) == 2 and region.isalpha():
        region = f"{region}-en"  # ISO code → DDG "us-en" form
    freshness = str(payload.get("freshness") or "").strip().lower() or None
    if freshness and freshness not in _FRESHNESS_TO_DDG_DF:
        return _err(
            "web_search.invalid_freshness",
            "freshness must be one of: pd, pw, pm, py.",
        )
    return query, count, mode, region, freshness


def _ddg_request(query: str, region: str, freshness: str | None) -> dict | str:
    """Side-effect: POST to DuckDuckGo's HTML endpoint; returns response.text or error envelope."""
    form_data = {"q": query, "kl": region}
    if freshness:
        form_data["df"] = _FRESHNESS_TO_DDG_DF[freshness]
    try:
        resp = httpx.post(
            _DDG_HTML_ENDPOINT,
            data=form_data,
            headers=_DDG_REQUEST_HEADERS,
            timeout=_TIMEOUT_S,
            follow_redirects=True,
        )
    except httpx.TimeoutException:
        return _err("web_search.timeout", "DuckDuckGo request timed out.")
    except httpx.RequestError as exc:
        return _err(
            "web_search.upstream_unreachable",
            f"DuckDuckGo request failed: {exc}",
        )
    if resp.status_code == 429:
        return _err(
            "web_search.rate_limited",
            "DuckDuckGo rate-limited this request. Retry shortly.",
        )
    if resp.status_code != 200:
        return _err(
            "web_search.upstream_error",
            f"DuckDuckGo returned HTTP {resp.status_code}.",
        )
    return resp.text


def _filter_safe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure-ish: drop rows with empty title/url and SSRF-validate every surviving URL.

    Why: DDG sanitises but cannot vouch for individual result URLs; the
    SSRF gate enforces CLAUDE.md's "all outbound URLs go through
    url_security.py" invariant on user-visible URLs too.
    """
    safe: list[dict[str, Any]] = []
    for r in results:
        if not (r["title"] and r["url"]):
            continue
        try:
            validate_outbound_url(r["url"], "result")
        except Exception:
            continue  # WHY: drop a single bad row instead of failing the whole call
        safe.append(r)
    return safe


def run(payload: dict) -> dict[str, Any]:
    """Search the web via DuckDuckGo's HTML endpoint and return ranked rows.

    Why: DDG's HTML endpoint is keyless and stable; ditching the paid Brave
    API removes a per-call vendor dependency for the marketplace listing.
    """
    parsed = _validate_run_inputs(payload or {})
    if isinstance(parsed, dict):
        return parsed
    query, count, mode, region, freshness = parsed
    body = _ddg_request(query, region, freshness)
    if isinstance(body, dict):
        return body  # error envelope
    try:
        results = _parse_ddg_html(body, count)
    except Exception as exc:  # WHY: parser bugs must not surface as a 500
        return _err(
            "web_search.parse_failed",
            f"Failed to parse DuckDuckGo HTML: {exc}",
        )
    safe_results = _filter_safe_results(results)
    return {
        "query": query,
        "mode": mode,
        "result_count": len(safe_results),
        "results": safe_results[:count],
        "billing_units_actual": 1,
    }
