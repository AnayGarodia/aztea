"""
web_search.py — Live web search via the Brave Search API.

Input:
  {
    "query": "what is aztea",          # required, 1-400 chars
    "count": 10,                         # optional, default 10, hard cap 20
    "mode": "web" | "news",             # optional, default "web"
    "country": "US",                    # optional, ISO-3166-1 alpha-2
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
        "age": str | null,             # human-readable, when provider supplies
        "site_name": str | null,
        "thumbnail_url": str | null
      }
    ],
    "billing_units_actual": int        # always 1
  }

Setup:
  Requires BRAVE_SEARCH_API_KEY env var. Free tier = 2k queries/month.
  When the key is absent the agent returns a structured error so the
  marketplace listing degrades cleanly instead of charging the caller.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from core.url_security import validate_outbound_url

_BRAVE_WEB_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_NEWS_ENDPOINT = "https://api.search.brave.com/res/v1/news/search"
_TIMEOUT_S = 8.0
_DEFAULT_COUNT = 10
_HARD_MAX_COUNT = 20
_MAX_QUERY_LEN = 400
_VALID_MODES = {"web", "news"}
_VALID_FRESHNESS = {"pd", "pw", "pm", "py"}


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _normalize_web_result(item: dict[str, Any]) -> dict[str, Any]:
    profile = item.get("profile") or {}
    thumbnail = item.get("thumbnail") or {}
    return {
        "title": str(item.get("title") or "").strip(),
        "url": str(item.get("url") or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "age": str(item.get("age") or "") or None,
        "site_name": str(profile.get("name") or "") or None,
        "thumbnail_url": str(thumbnail.get("src") or "") or None,
    }


def _normalize_news_result(item: dict[str, Any]) -> dict[str, Any]:
    thumbnail = item.get("thumbnail") or {}
    meta_url = item.get("meta_url") or {}
    return {
        "title": str(item.get("title") or "").strip(),
        "url": str(item.get("url") or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "age": str(item.get("age") or "") or None,
        "site_name": str(meta_url.get("hostname") or "") or None,
        "thumbnail_url": str(thumbnail.get("src") or "") or None,
    }


def run(payload: dict) -> dict:
    """Run a live web search via the Brave Search API.

    Cheap, single-shot. Returns up to 20 result objects with title, url,
    description, optional age + thumbnail. News mode is also available.
    """
    query = str(payload.get("query") or "").strip()
    if not query:
        return _err("web_search.missing_query", "query is required")
    if len(query) > _MAX_QUERY_LEN:
        return _err(
            "web_search.query_too_long",
            f"query must be ≤ {_MAX_QUERY_LEN} chars (got {len(query)})",
        )

    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return _err(
            "web_search.no_api_key",
            "BRAVE_SEARCH_API_KEY is not configured on this server. "
            "Set the env var to enable live web search (free tier at brave.com/search/api).",
        )

    mode = str(payload.get("mode") or "web").strip().lower()
    if mode not in _VALID_MODES:
        return _err(
            "web_search.invalid_mode",
            f"mode must be one of: {sorted(_VALID_MODES)}",
        )

    try:
        count = int(payload.get("count") or _DEFAULT_COUNT)
    except (TypeError, ValueError):
        count = _DEFAULT_COUNT
    count = max(1, min(count, _HARD_MAX_COUNT))

    params: dict[str, Any] = {"q": query, "count": count}

    country = str(payload.get("country") or "").strip().upper()
    if country:
        if len(country) != 2 or not country.isalpha():
            return _err(
                "web_search.invalid_country",
                "country must be a 2-letter ISO-3166-1 alpha-2 code (e.g. US, GB)",
            )
        params["country"] = country

    freshness = str(payload.get("freshness") or "").strip().lower()
    if freshness:
        if freshness not in _VALID_FRESHNESS:
            return _err(
                "web_search.invalid_freshness",
                f"freshness must be one of: {sorted(_VALID_FRESHNESS)}",
            )
        params["freshness"] = freshness

    endpoint = _BRAVE_NEWS_ENDPOINT if mode == "news" else _BRAVE_WEB_ENDPOINT

    try:
        resp = httpx.get(
            endpoint,
            params=params,
            timeout=_TIMEOUT_S,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
                "User-Agent": "Aztea-Web-Search/1.0",
            },
        )
    except httpx.HTTPError as exc:
        return _err(
            "web_search.fetch_failed",
            f"Brave API request failed: {type(exc).__name__}: {exc}",
        )

    if resp.status_code == 401 or resp.status_code == 403:
        return _err(
            "web_search.auth_failed",
            f"Brave API rejected the API key (HTTP {resp.status_code}). "
            "Verify BRAVE_SEARCH_API_KEY is correct.",
        )
    if resp.status_code == 429:
        return _err(
            "web_search.rate_limited",
            "Brave API rate-limited this query. Try again in a few seconds, "
            "or upgrade the subscription tier.",
        )
    if resp.status_code >= 400:
        return _err(
            "web_search.upstream_error",
            f"Brave API returned HTTP {resp.status_code}: {resp.text[:300]}",
        )

    try:
        data = resp.json()
    except ValueError:
        return _err(
            "web_search.parse_failed",
            "Brave API returned non-JSON body",
        )

    if mode == "web":
        items = (data.get("web") or {}).get("results") or []
        results = [_normalize_web_result(item) for item in items if isinstance(item, dict)]
    else:
        items = (data.get("results") or [])
        results = [_normalize_news_result(item) for item in items if isinstance(item, dict)]

    # Drop any blanks (provider occasionally returns rows without title/url).
    results = [r for r in results if r["title"] and r["url"]]

    # SSRF guardrail on every URL we surface back to the caller. Brave is a
    # trusted upstream, but CLAUDE.md's "all outbound URLs go through
    # url_security.py" invariant covers result URLs too — if Brave ever
    # returns a private/loopback/reserved address (compromised result,
    # parked-domain redirect, etc.) the caller should not see it.
    safe_results = []
    for r in results:
        try:
            validate_outbound_url(r["url"], "result")
            safe_results.append(r)
        except Exception:
            # Drop the row silently rather than fail the whole call.
            continue
    results = safe_results

    return {
        "query": query,
        "mode": mode,
        "result_count": len(results),
        "results": results[:count],
        "billing_units_actual": 1,
    }
