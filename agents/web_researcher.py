"""
web_researcher.py — Fetch and intelligently synthesize web page content

Input:  {
  "url": "https://example.com/article",
  "question": "What is the main argument?",   # optional focus question
  "mode": "summary"   # summary | extract | qa
}
Output: {
  "url": str,
  "title": str,
  "word_count": int,
  "fetched_at": str,
  "summary": str,
  "key_points": [str],
  "answer": str,           # direct answer to question if provided
  "quotes": [str],         # verbatim supporting quotes
  "links": [{"text": str, "href": str}],
  "content_type": str      # article | documentation | product_page | forum | etc.
}
"""

import re
import html
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from core.llm import CompletionRequest, Message, run_with_fallback

_FETCH_TIMEOUT = 15
_MAX_CONTENT_CHARS = 12000
_MAX_URL_LENGTH = 2048

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "169.254.169.254",  # AWS metadata
    "metadata.google.internal",
}

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


def _validate_url(url: str) -> str | None:
    if len(url) > _MAX_URL_LENGTH:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return None
    host = parsed.hostname or ""
    if host.lower() in _BLOCKED_HOSTS:
        return None
    # Block private IP ranges
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return None
    except ValueError:
        pass  # hostname, not IP
    return url


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


def run(payload: dict) -> dict:
    url = str(payload.get("url", "")).strip()
    if not url:
        return {"error": "url is required"}

    safe_url = _validate_url(url)
    if safe_url is None:
        return {"error": "URL is invalid or not allowed (must be public http/https)"}

    question = str(payload.get("question", "")).strip()
    mode = str(payload.get("mode", "summary")).lower()
    if mode not in ("summary", "extract", "qa"):
        mode = "summary"

    # Fetch
    try:
        resp = requests.get(
            safe_url,
            timeout=_FETCH_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; aztea-web-researcher/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
            allow_redirects=True,
        )
        resp.raise_for_status()
        raw_html = resp.text
    except requests.exceptions.Timeout:
        return {"error": "Request timed out fetching the URL"}
    except requests.exceptions.HTTPError as e:
        return {"error": f"HTTP {e.response.status_code} fetching URL"}
    except Exception as e:
        return {"error": f"Failed to fetch URL: {type(e).__name__}"}

    text, links, html_title = _strip_html(raw_html)
    word_count = len(text.split())
    fetched_at = datetime.now(timezone.utc).isoformat()

    truncated = text[:_MAX_CONTENT_CHARS]

    req = CompletionRequest(
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(role="user", content=_USER.format(
                url=url,
                question=question or "Provide a general analysis.",
                mode=mode,
                content=truncated,
            )),
        ],
        temperature=0.15,
        max_tokens=900,
    )
    raw = run_with_fallback(req)
    text_out = raw.text.strip()
    text_out = re.sub(r"^```(?:json)?\s*", "", text_out)
    text_out = re.sub(r"\s*```$", "", text_out)

    import json
    try:
        llm_data = json.loads(text_out)
    except Exception:
        llm_data = {
            "title": html_title,
            "summary": text_out[:400],
            "key_points": [],
            "answer": "",
            "quotes": [],
            "content_type": "other",
        }

    return {
        "url": url,
        "title": llm_data.get("title") or html_title,
        "word_count": word_count,
        "fetched_at": fetched_at,
        "summary": llm_data.get("summary", ""),
        "key_points": llm_data.get("key_points", []),
        "answer": llm_data.get("answer", ""),
        "quotes": llm_data.get("quotes", []),
        "links": links,
        "content_type": llm_data.get("content_type", "other"),
    }
