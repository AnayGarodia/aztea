"""
docs_grounder.py — Fetch current official documentation for any library/framework.

Solves Claude's biggest failure mode: hallucinating APIs from stale training data.
Fetches live docs, extracts API signatures, code examples, and gotchas, then
synthesises a focused answer to the caller's specific question.

Input:
  {
    "library":  "stripe",              # required — e.g. "nextjs", "react", "prisma"
    "question": "how do webhooks work",# optional — scopes synthesis to a specific topic
    "version":  "v3"                   # optional — e.g. "latest", "13.4", "v3"
  }

Output (success):
  {
    "library":         str,
    "version_found":   str,
    "summary":         str,
    "code_example":    str,
    "api_signatures":  [str],
    "gotchas":         [str],
    "sources":         [{"url": str, "title": str, "excerpt": str}],
    "as_of_date":      str,
    "query_used":      str
  }

Output (error):
  {"error": {"code": "docs_grounder.not_found",        "message": "..."}}
  {"error": {"code": "docs_grounder.synthesis_failed", "message": "..."}}
"""

# OWNS: fetching and synthesising live official documentation for libraries/frameworks
# NOT OWNS: general web search (delegated to agents.web_search), code execution,
#           version management, or package installation advice
# INVARIANTS:
#   - Only fetch URLs that come back from the web search results (no caller-supplied URLs)
#   - Every outbound URL must be http or https; others are silently skipped
#   - HTTP requests are bounded to _HTTP_TIMEOUT_S seconds each
#   - If LLM synthesis fails, return raw fetched content rather than raising
# DECISIONS:
#   - We fetch top 3 search results and pass the combined text to one LLM call rather
#     than summarising each page separately — gives the model cross-page context at
#     the cost of a larger prompt. Switch to per-page if token budgets become an issue.
#   - Source ranking (official > github > npm/pypi > stackoverflow) is implemented by
#     re-ordering search results, not by filtering — we keep all sources but prefer the
#     official domain in the LLM context ordering.
# KNOWN DEBT:
#   - We strip HTML with a regex which is inherently fragile; a proper HTML parser
#     (html.parser or lxml) would be more robust for deeply-nested docs sites.

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timezone
from urllib.parse import urlparse

import requests

import agents.web_search as _web_search
from core.llm import CompletionRequest, Message, run_with_fallback
from core.llm.errors import LLMError
from agents._contracts import agent_error as _err

_HTTP_TIMEOUT_S = 10
_MAX_CHARS_PER_PAGE = 8_000   # chars sent to LLM per fetched page
_MAX_PAGES_TO_FETCH = 3
_MAX_SEARCH_RESULTS = 5

# Browser-like User-Agent so docs CDNs don't block the request.
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_RE_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_HORIZ_WS = re.compile(r"[ \t]+")
_RE_VERT_WS = re.compile(r"\n{3,}")

_SYNTHESIS_SYSTEM = """\
You are a senior engineer who reads official library documentation daily. \
Given raw documentation text fetched from the library's official docs, \
extract a focused, accurate answer to the user's question.

Return only valid JSON — no markdown fences, no extra commentary."""

_SYNTHESIS_USER = """\
Library: {library}
Version hint: {version}
User question: {question}

Documentation text (from top sources):
{docs_text}

Return JSON with exactly these keys:
{{
  "version_found": "the version the docs describe (or 'unknown' if not found)",
  "summary": "2-4 sentence direct answer to the user question based on the docs",
  "code_example": "the most relevant code snippet verbatim from the docs, or empty string",
  "api_signatures": ["key function/method/class signatures mentioned in the docs"],
  "gotchas": ["common mistakes, breaking changes, or migration notes found in the docs"]
}}"""



def _is_safe_http_url(url: str) -> bool:
    """Accept only http/https URLs; reject anything else (data:, file:, etc.)."""
    try:
        scheme = urlparse(url).scheme.lower()
        return scheme in ("http", "https")
    except Exception:
        return False


def _source_rank(url: str, library: str) -> int:
    """Lower rank = higher priority. Official docs domain scores best."""
    host = (urlparse(url).hostname or "").lower()
    lib_lower = library.lower()
    # Official documentation domains (e.g. docs.stripe.com, nextjs.org, reactjs.org)
    if f"docs.{lib_lower}" in host or f"{lib_lower}.org" in host or f"{lib_lower}.dev" in host:
        return 0
    if f"{lib_lower}.com" in host or f"{lib_lower}.io" in host:
        return 1
    # Official GitHub org or repo
    if "github.com" in host and lib_lower in url.lower():
        return 2
    # Package registries
    if "npmjs.com" in host or "pypi.org" in host:
        return 3
    # Q&A sites — useful but deprioritised vs official sources
    if "stackoverflow.com" in host or "stackexchange.com" in host:
        return 5
    return 4


def _strip_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace into plain readable text.

    Not a full parser — handles the common case of docs pages well enough
    for LLM synthesis. See KNOWN DEBT above.
    """
    text = _RE_SCRIPT_STYLE.sub("", html)
    text = _RE_TAG.sub(" ", text)
    text = _RE_HORIZ_WS.sub(" ", text)
    text = _RE_VERT_WS.sub("\n\n", text)
    # Decode common HTML entities (full parser is overkill for docs-page extraction).
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return text.strip()


def _fetch_page(url: str) -> str | None:
    """Fetch a single URL and return stripped plain text, or None on failure."""
    if not _is_safe_http_url(url):
        return None
    try:
        resp = requests.get(url, headers=_FETCH_HEADERS, timeout=_HTTP_TIMEOUT_S)
        if resp.status_code != 200:
            return None
        return _strip_html(resp.text)
    except requests.exceptions.Timeout:
        return None
    except requests.exceptions.RequestException:
        return None


def _build_search_query(library: str, version: str, question: str) -> str:
    """Build a targeted search query that prefers official documentation sources."""
    parts = [library]
    if version and version.lower() not in ("latest", ""):
        parts.append(version)
    parts.append("documentation")
    if question:
        # Append only the first ~60 chars of the question so the query stays focused.
        parts.append(question[:60])
    # Bias the search engine toward the official docs domain and GitHub.
    parts.append(f"site:docs.{library}.com OR site:github.com/{library}")
    return " ".join(parts)


def _rank_and_deduplicate(results: list[dict], library: str) -> list[dict]:
    """Sort search results by source quality and drop duplicate hosts."""
    seen_hosts: set[str] = set()
    unique: list[dict] = []
    for r in results:
        host = (urlparse(r.get("url", "")).hostname or "").lower()
        if host and host not in seen_hosts:
            seen_hosts.add(host)
            unique.append(r)
    unique.sort(key=lambda r: _source_rank(r.get("url", ""), library))
    return unique


def _synthesise(
    library: str,
    version: str,
    question: str,
    docs_text: str,
) -> dict | None:
    """Call the LLM to extract structured information from raw docs text.

    Returns a parsed dict on success, or None if the LLM is unavailable or
    returns unparseable output (caller handles graceful degradation).
    """
    prompt = _SYNTHESIS_USER.format(
        library=library,
        version=version or "any",
        question=question or "general overview and usage",
        docs_text=docs_text[:20_000],  # hard cap to avoid oversized prompts
    )
    req = CompletionRequest(
        model="",
        messages=[
            Message(role="system", content=_SYNTHESIS_SYSTEM),
            Message(role="user", content=prompt),
        ],
        temperature=0.1,
        max_tokens=1200,
    )
    try:
        raw = run_with_fallback(req)
        text = raw.text.strip()
        # Strip markdown fences in case the model ignored the instruction.
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except (LLMError, json.JSONDecodeError, Exception):
        return None


def run(payload: dict) -> dict:
    """Fetch current official documentation for a library and answer a question about it."""
    payload = payload or {}

    library = str(payload.get("library") or "").strip()
    if not library:
        return _err("docs_grounder.missing_library", "library is required.")
    if len(library) > 100:
        return _err("docs_grounder.invalid_library", "library name is too long (max 100 chars).")

    question = str(payload.get("question") or "").strip()
    version = str(payload.get("version") or "").strip()

    today = date.today().isoformat()
    query = _build_search_query(library, version, question)

    # --- Step 1: search for documentation pages ---
    search_result = _web_search.run({"query": query, "count": _MAX_SEARCH_RESULTS})
    if "error" in search_result:
        return _err(
            "docs_grounder.not_found",
            f"Web search failed for library '{library}': {search_result['error'].get('message', '')}",
        )

    raw_results: list[dict] = search_result.get("results") or []
    if not raw_results:
        return _err(
            "docs_grounder.not_found",
            f"No documentation found for '{library}'. Try a more specific library name.",
        )

    # --- Step 2: rank by source quality and drop duplicate hosts ---
    ranked = _rank_and_deduplicate(raw_results, library)

    # --- Step 3: fetch and strip top pages (parallel; order preserved) ---
    candidates = [
        r for r in ranked[:_MAX_PAGES_TO_FETCH]
        if _is_safe_http_url(r.get("url", ""))
    ]
    sources: list[dict] = []
    fetched_texts: list[str] = []
    if candidates:
        urls = [c.get("url", "") for c in candidates]
        with ThreadPoolExecutor(max_workers=min(_MAX_PAGES_TO_FETCH, len(urls))) as pool:
            page_texts = list(pool.map(_fetch_page, urls))
        for result, page_text in zip(candidates, page_texts):
            url = result.get("url", "")
            title = result.get("title", "")
            description = result.get("description", "")
            if page_text:
                excerpt = page_text[:300].replace("\n", " ").strip()
                fetched_texts.append(page_text[:_MAX_CHARS_PER_PAGE])
            else:
                excerpt = description[:300]
            sources.append({"url": url, "title": title, "excerpt": excerpt})

    if not fetched_texts and not sources:
        return _err(
            "docs_grounder.not_found",
            f"Found search results for '{library}' but could not fetch any documentation pages.",
        )

    combined_docs = "\n\n---\n\n".join(fetched_texts) if fetched_texts else ""

    # --- Step 4: LLM synthesis ---
    synthesis = None
    if combined_docs:
        synthesis = _synthesise(library, version, question, combined_docs)

    if synthesis is not None:
        version_found = str(synthesis.get("version_found") or version or "unknown")
        summary = str(synthesis.get("summary") or "")
        code_example = str(synthesis.get("code_example") or "")
        api_signatures = [str(s) for s in (synthesis.get("api_signatures") or []) if s]
        gotchas = [str(g) for g in (synthesis.get("gotchas") or []) if g]
    else:
        # Graceful degradation: LLM unavailable or returned unparseable output.
        # Still return the raw fetched content so the caller gets something useful.
        raw_summary = combined_docs[:600] if combined_docs else (
            sources[0]["excerpt"] if sources else ""
        )
        version_found = version or "unknown"
        summary = (
            f"LLM synthesis unavailable. Raw documentation excerpt:\n\n{raw_summary}"
        )
        code_example = ""
        api_signatures = []
        gotchas = []

    if not summary and not sources:
        return _err(
            "docs_grounder.not_found",
            f"Could not retrieve documentation for '{library}'.",
        )

    return {
        "library": library,
        "version_found": version_found,
        "summary": summary,
        "code_example": code_example,
        "api_signatures": api_signatures,
        "gotchas": gotchas,
        "sources": sources,
        "as_of_date": today,
        "query_used": query,
    }
