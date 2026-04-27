"""
web_researcher.py — Fetch and intelligently synthesize web page content

Input:  {
  "url": "https://example.com/article",          # single URL (legacy)
  "urls": ["https://...", "https://..."],         # multi-URL (max 10)
  "question": "What is the main argument?",       # optional focus question
  "mode": "summary"   # summary | extract | qa
}
Output (single URL, backward-compat): {
  "url": str,
  "title": str,
  "word_count": int,
  "fetched_at": str,
  "summary": str,
  "key_points": [str],
  "answer": str,
  "quotes": [str],
  "links": [{"text": str, "href": str}],
  "content_type": str,
  "per_url_findings": [...],
  "synthesis": str,
  "cross_source_consensus": null,
  "billing_units_actual": int,
}
Output (multi URL): {
  "urls": [str],
  "per_url_findings": [{"url": str, "status": "ok"/"error", "content_length": int}],
  "synthesis": str,
  "cross_source_consensus": str | null,
  "billing_units_actual": int,
}
"""

import re
import html
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from core.llm import CompletionRequest, Message, run_with_fallback
from core.url_security import validate_outbound_url

_FETCH_TIMEOUT = 15
_MAX_CONTENT_CHARS = 12000
_MAX_URL_LENGTH = 2048
_MAX_URLS = 10

_SYSTEM = """\
You are an expert research analyst. Given the text content of a web page, produce a structured analysis.

Return only valid JSON — no markdown fences, no prose outside the JSON."""

_USER = """\
URL: {url}
Question to answer: {question}
Mode: {mode}

Page content:
{content}

Return JSON with exactly:
{{
  "title": "page title or inferred title",
  "summary": "2-4 sentence dense summary of the page content",
  "key_points": ["3-6 specific, evidence-backed key points from the page"],
  "answer": "direct answer to the question asked, with supporting evidence (empty string if no question)",
  "quotes": ["1-3 verbatim short quotes (under 150 chars) that best support the key points"],
  "content_type": "article|documentation|product_page|forum|blog|news|academic|other"
}}"""

_MULTI_SYSTEM = """\
You are an expert research analyst. Given text content from multiple web pages, produce a structured synthesis.

Return only valid JSON — no markdown fences, no prose outside the JSON."""

_MULTI_USER = """\
Question to answer: {question}
Mode: {mode}

Content from {n_sources} sources:
{content}

Return JSON with exactly:
{{
  "synthesis": "3-5 sentence synthesis across all sources, noting agreements and differences",
  "key_points": ["4-8 key points drawn from across the sources"],
  "answer": "direct answer to the question based on all sources (empty string if no question)",
  "cross_source_consensus": "one sentence describing what all sources agree on"
}}"""


def _strip_html(raw_html: str) -> tuple[str, list[dict], str]:
    """Minimal HTML→text: strip tags, decode entities, extract links and title."""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
    title = html.unescape(title_m.group(1).strip()) if title_m else ""

    # Remove scripts, styles, nav, footer
    cleaned = re.sub(r"<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)

    # Extract links before stripping
    links = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', cleaned, re.IGNORECASE | re.DOTALL):
        href = m.group(1).strip()
        link_text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if href.startswith("http") and link_text:
            links.append({"text": link_text[:100], "href": href[:200]})

    # Strip all tags
    text = re.sub(r"<[^>]+>", " ", cleaned)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    return text, links[:20], title


def _fetch_one(url: str) -> dict:
    """Fetch a single URL and return stripped text or error info."""
    try:
        safe_url = validate_outbound_url(url, "url")
    except ValueError:
        return {"url": url, "content": None, "status": "error", "error": "URL is invalid or not allowed (must be public http/https)"}

    try:
        resp = requests.get(
            safe_url,
            timeout=_FETCH_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; aztea-web-researcher/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
            allow_redirects=True,
            stream=True,
        )
        resp.raise_for_status()
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536, decode_unicode=False):
            total += len(chunk)
            if total > 5 * 1024 * 1024:
                return {"url": url, "content": None, "status": "error", "error": "Page is too large to process (over 5 MB)"}
            chunks.append(chunk)
        raw_html = b"".join(chunks).decode("utf-8", errors="replace")
    except requests.exceptions.Timeout:
        return {"url": url, "content": None, "status": "error", "error": "Request timed out fetching the URL"}
    except requests.exceptions.HTTPError as e:
        return {"url": url, "content": None, "status": "error", "error": f"HTTP {e.response.status_code} fetching URL"}
    except Exception as e:
        return {"url": url, "content": None, "status": "error", "error": f"Failed to fetch URL: {type(e).__name__}"}

    text, links, html_title = _strip_html(raw_html)

    # Detect JS-rendered SPAs: page has a root mount point but essentially no text content
    is_spa = (
        bool(re.search(r'<div\s+id=["\'](?:root|app|__next)["\']', raw_html, re.IGNORECASE))
        and len(text.strip()) < 200
    )
    if is_spa:
        return {
            "url": url,
            "content": None,
            "status": "error",
            "error": "js_rendered — page is a JavaScript SPA; static fetch returned no readable content",
        }

    return {
        "url": url,
        "content": text,
        "status": "ok",
        "links": links,
        "html_title": html_title,
        "word_count": len(text.split()),
    }


def _parse_llm_json(text_out: str, fallback: dict) -> dict:
    text_out = re.sub(r"^```(?:json)?\s*", "", text_out.strip())
    text_out = re.sub(r"\s*```$", "", text_out)
    try:
        return json.loads(text_out)
    except Exception:
        return fallback


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def run(payload: dict) -> dict:
    question = str(payload.get("question", "")).strip()
    if len(question) > 1000:
        return _err("web_researcher.invalid_input", "question must be 1000 characters or fewer")

    mode = str(payload.get("mode", "summary")).lower()
    if mode not in ("summary", "extract", "qa"):
        mode = "summary"

    # Determine URL list
    urls_raw = payload.get("urls")
    url_single = str(payload.get("url", "")).strip()
    multi_mode = False

    if urls_raw is not None and len(urls_raw) > 0:
        if len(urls_raw) > _MAX_URLS:
            return _err("web_researcher.too_many_urls", f"urls must contain at most {_MAX_URLS} URLs, got {len(urls_raw)}")
        urls = [str(u).strip() for u in urls_raw]
        multi_mode = True
    elif url_single:
        if len(url_single) > _MAX_URL_LENGTH:
            return _err("web_researcher.invalid_url", "URL is invalid or not allowed (must be public http/https)")
        urls = [url_single]
        multi_mode = False
    else:
        return _err("web_researcher.missing_url", "url or urls is required")

    # Validate URL lengths upfront (security check deferred to _fetch_one)
    for u in urls:
        if len(u) > _MAX_URL_LENGTH:
            return _err("web_researcher.url_too_long", f"URL too long (max {_MAX_URL_LENGTH} chars): {u[:80]}...")

    # Parallel fetch
    fetched_at = datetime.now(timezone.utc).isoformat()
    results_map: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=5) as pool:
        future_to_url = {pool.submit(_fetch_one, u): u for u in urls}
        for future in as_completed(future_to_url):
            try:
                res = future.result()
            except Exception as exc:
                u = future_to_url[future]
                res = {"url": u, "content": None, "status": "error", "error": f"Unexpected fetch error: {type(exc).__name__}"}
            results_map[res["url"]] = res

    # Preserve original URL order
    fetch_results = [results_map[u] for u in urls]

    successful = [r for r in fetch_results if r["status"] == "ok"]
    billing_units_actual = len(successful)

    # per_url_findings — billing info only, no raw content
    per_url_findings = [
        {
            "url": r["url"],
            "status": r["status"],
            "content_length": len(r["content"]) if r.get("content") else 0,
            **({"error": r["error"]} if r["status"] == "error" else {}),
        }
        for r in fetch_results
    ]

    # --- Single URL mode (backward-compat) ---
    if not multi_mode:
        r = fetch_results[0]
        if r["status"] == "error":
            return _err("web_researcher.fetch_failed", r["error"])

        text = r["content"]
        links = r.get("links", [])
        html_title = r.get("html_title", "")
        word_count = r.get("word_count", 0)
        truncated = text[:_MAX_CONTENT_CHARS]

        req = CompletionRequest(
            model="",
            messages=[
                Message(role="system", content=_SYSTEM),
                Message(role="user", content=_USER.format(
                    url=url_single,
                    question=question or "Provide a general analysis.",
                    mode=mode,
                    content=truncated,
                )),
            ],
            temperature=0.15,
            max_tokens=900,
        )
        raw = run_with_fallback(req)
        llm_data = _parse_llm_json(raw.text, {
            "title": html_title,
            "summary": raw.text[:400],
            "key_points": [],
            "answer": "",
            "quotes": [],
            "content_type": "other",
        })

        synthesis = llm_data.get("summary", "")

        return {
            "url": url_single,
            "title": llm_data.get("title") or html_title,
            "word_count": word_count,
            "fetched_at": fetched_at,
            "summary": llm_data.get("summary", ""),
            "key_points": llm_data.get("key_points", []),
            "answer": llm_data.get("answer", ""),
            "quotes": llm_data.get("quotes", []),
            "links": links,
            "content_type": llm_data.get("content_type", "other"),
            # New fields
            "per_url_findings": per_url_findings,
            "synthesis": synthesis,
            "cross_source_consensus": None,
            "billing_units_actual": billing_units_actual,
        }

    # --- Multi URL mode ---
    if not successful:
        return {
            "urls": urls,
            "per_url_findings": per_url_findings,
            "synthesis": "",
            "cross_source_consensus": None,
            "billing_units_actual": 0,
            "error": "All URLs failed to fetch",
        }

    # Build combined content for LLM
    combined_parts = []
    for r in successful:
        truncated = r["content"][:_MAX_CONTENT_CHARS // len(successful)]
        combined_parts.append(f"--- Source: {r['url']} ---\n{truncated}")
    combined_content = "\n\n".join(combined_parts)

    req = CompletionRequest(
        model="",
        messages=[
            Message(role="system", content=_MULTI_SYSTEM),
            Message(role="user", content=_MULTI_USER.format(
                question=question or "Provide a general analysis.",
                mode=mode,
                n_sources=len(successful),
                content=combined_content,
            )),
        ],
        temperature=0.15,
        max_tokens=1200,
    )
    raw = run_with_fallback(req)
    llm_data = _parse_llm_json(raw.text, {
        "synthesis": raw.text[:600],
        "key_points": [],
        "answer": "",
        "cross_source_consensus": "",
    })

    cross_source_consensus = llm_data.get("cross_source_consensus") if len(successful) > 1 else None

    return {
        "urls": urls,
        "per_url_findings": per_url_findings,
        "synthesis": llm_data.get("synthesis", ""),
        "key_points": llm_data.get("key_points", []),
        "answer": llm_data.get("answer", ""),
        "cross_source_consensus": cross_source_consensus,
        "billing_units_actual": billing_units_actual,
        "fetched_at": fetched_at,
    }
