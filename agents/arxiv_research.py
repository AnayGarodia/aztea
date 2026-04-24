"""
arxiv_research.py — Real academic paper search via arXiv API

Input:  {
  "query": "transformer attention mechanism",
  "max_results": 8,          # 1-20
  "sort_by": "relevance",    # relevance | lastUpdatedDate | submittedDate
  "categories": ["cs.AI"]    # optional arXiv category filters
}
Output: {
  "query": str,
  "total_found": int,
  "papers": [{
    "arxiv_id": str,
    "title": str,
    "authors": [str],
    "abstract": str,
    "categories": [str],
    "published": str,
    "updated": str,
    "pdf_url": str,
    "abstract_url": str
  }],
  "synthesis": str,
  "key_themes": [str],
  "seminal_papers": [str],
  "open_questions": [str],
  "suggested_follow_ups": [str]
}
"""

import re
import xml.etree.ElementTree as ET

import httpx
import requests

from core.llm import CompletionRequest, Message, run_with_fallback

_ARXIV_API = "https://export.arxiv.org/api/query"
_TIMEOUT = 15
_NS = "http://www.w3.org/2005/Atom"

_SYNTHESIS_SYSTEM = """\
You are a research scientist who reads arXiv papers daily. Given a set of paper titles and abstracts,
produce a dense synthesis of the literature: key themes, what's converging, what's contested, seminal
works in the set, and open questions the field hasn't answered yet.

Return only valid JSON — no markdown fences."""

_SYNTHESIS_USER = """\
Query: {query}

Papers:
{papers_text}

Return JSON with exactly:
{{
  "synthesis": "2-4 sentence expert summary of this literature",
  "key_themes": ["3-6 recurring themes or techniques across papers"],
  "seminal_papers": ["arxiv IDs of the 1-3 most foundational papers in this result set"],
  "open_questions": ["2-4 research questions these papers leave unanswered"],
  "suggested_follow_ups": ["2-3 related search queries worth exploring next"]
}}"""


def _fetch_arxiv(query: str, max_results: int, sort_by: str, categories: list[str]) -> list[dict]:
    search_query = query
    if categories:
        cat_filter = " OR ".join(f"cat:{c}" for c in categories[:5])
        search_query = f"({query}) AND ({cat_filter})"

    sort_order_map = {
        "relevance": "relevance",
        "lastUpdatedDate": "lastUpdatedDate",
        "submittedDate": "submittedDate",
    }
    sort_order = sort_order_map.get(sort_by, "relevance")

    try:
        resp = requests.get(
            _ARXIV_API,
            params={
                "search_query": f"all:{search_query}",
                "start": 0,
                "max_results": min(max_results, 20),
                "sortBy": sort_order,
                "sortOrder": "descending",
            },
            timeout=_TIMEOUT,
            headers={"User-Agent": "aztea-arxiv-agent/1.0"},
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        raise RuntimeError("arXiv API timed out. Try again in a moment.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"arXiv API returned an error ({e.response.status_code}). Try again later.")
    except Exception as e:
        raise RuntimeError(f"Could not reach arXiv API: {type(e).__name__}.")

    root = ET.fromstring(resp.text)
    papers = []

    for entry in root.findall(f"{{{_NS}}}entry"):
        def _text(tag):
            el = entry.find(f"{{{_NS}}}{tag}")
            return el.text.strip() if el is not None and el.text else ""

        arxiv_id_full = _text("id")
        arxiv_id = arxiv_id_full.split("/abs/")[-1] if "/abs/" in arxiv_id_full else arxiv_id_full

        authors = [
            a.findtext(f"{{{_NS}}}name", "").strip()
            for a in entry.findall(f"{{{_NS}}}author")
        ]

        categories_list = [
            c.get("term", "")
            for c in entry.findall("{http://arxiv.org/schemas/atom}primary_category")
        ]
        if not categories_list:
            categories_list = [
                c.get("term", "")
                for c in entry.findall("{http://www.w3.org/2005/Atom}category")
            ]

        pdf_url = ""
        abs_url = ""
        for link in entry.findall(f"{{{_NS}}}link"):
            href = link.get("href", "")
            if link.get("title") == "pdf":
                pdf_url = href
            elif link.get("rel") == "alternate":
                abs_url = href

        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        if not abs_url and arxiv_id:
            abs_url = f"https://arxiv.org/abs/{arxiv_id}"

        abstract = re.sub(r"\s+", " ", _text("summary")).strip()

        papers.append({
            "arxiv_id": arxiv_id,
            "title": _text("title"),
            "authors": authors[:6],
            "abstract": abstract[:600],
            "categories": [c for c in categories_list if c][:4],
            "published": _text("published")[:10],
            "updated": _text("updated")[:10],
            "pdf_url": pdf_url,
            "abstract_url": abs_url,
        })

    return papers


_VALID_SORT_BY = {"relevance", "lastUpdatedDate", "submittedDate"}


def _extract_arxiv_id_from_url(id_url: str) -> str:
    """Extract plain arxiv id (no version) from a URL like http://arxiv.org/abs/2301.12345v1."""
    segment = id_url.rstrip("/").split("/")[-1]
    # Strip version suffix e.g. v1, v2
    segment = re.sub(r"v\d+$", "", segment)
    return segment


def _fetch_full_abstract_data(arxiv_id: str) -> dict:
    """Fetch the HTML abstract page for an arxiv paper and extract affiliations + related links."""
    url = f"https://arxiv.org/abs/{arxiv_id}"
    affiliations: list[str] = []
    related_links: list[str] = []
    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        # Extract author affiliations from <meta name="citation_author_institution" ...>
        affiliations = re.findall(
            r'<meta\s+name=["\']citation_author_institution["\']\s+content=["\'](.*?)["\']',
            html,
            re.IGNORECASE,
        )
        if not affiliations:
            # Fallback: text inside <div class="authors"> tags
            authors_div_match = re.search(
                r'<div[^>]+class=["\'][^"\']*authors[^"\']*["\'][^>]*>(.*?)</div>',
                html,
                re.IGNORECASE | re.DOTALL,
            )
            if authors_div_match:
                raw = re.sub(r"<[^>]+>", "", authors_div_match.group(1))
                affiliations = [a.strip() for a in raw.split(",") if a.strip()][:6]

        # Extract DOI and related paper links
        doi_matches = re.findall(r'https?://(?:dx\.)?doi\.org/[^\s"\'<>]+', html)
        related_links.extend(doi_matches)
        # Also grab any extra-ref-link hrefs
        extra_links = re.findall(
            r'<div[^>]+class=["\'][^"\']*extra-ref-link[^"\']*["\'][^>]*>.*?href=["\'](.*?)["\']',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        related_links.extend(extra_links)
        # Deduplicate and keep unique
        seen: set[str] = set()
        deduped: list[str] = []
        for lnk in related_links:
            if lnk not in seen:
                seen.add(lnk)
                deduped.append(lnk)
        related_links = deduped[:10]
    except Exception:
        pass  # Best-effort; don't fail the whole call

    return {"affiliations": affiliations[:10], "related_links": related_links}


def run(payload: dict) -> dict:
    query = str(payload.get("query", "")).strip()
    if not query:
        return {"error": "query is required"}
    if len(query) > 500:
        return {"error": "query must be 500 characters or fewer"}

    try:
        max_results = max(1, min(int(payload.get("max_results", 8)), 20))
    except (TypeError, ValueError):
        return {"error": "max_results must be a number between 1 and 20"}

    sort_by = str(payload.get("sort_by", "relevance"))
    if sort_by not in _VALID_SORT_BY:
        sort_by = "relevance"

    raw_categories = payload.get("categories") or []
    if not isinstance(raw_categories, list):
        return {"error": "categories must be a list of arXiv category strings (e.g. [\"cs.AI\"])"}
    categories = [str(c).strip() for c in raw_categories if str(c).strip()][:10]

    full_abstract = bool(payload.get("full_abstract", False))

    try:
        papers = _fetch_arxiv(query, max_results, sort_by, categories)
    except RuntimeError as exc:
        return {"error": str(exc)}

    if not papers:
        return {
            "query": query,
            "total_found": 0,
            "papers": [],
            "synthesis": "No papers found for this query. Try broader search terms.",
            "key_themes": [],
            "seminal_papers": [],
            "open_questions": [],
            "suggested_follow_ups": [],
            "billing_units_actual": 0,
        }

    # full_abstract mode: enrich top 3 papers with affiliation + related link data
    if full_abstract:
        for paper in papers[:3]:
            raw_id = paper.get("arxiv_id", "")
            # arxiv_id may already be stripped or may include version; normalise
            clean_id = _extract_arxiv_id_from_url(raw_id) if raw_id else ""
            if clean_id:
                paper["full_abstract_data"] = _fetch_full_abstract_data(clean_id)

    papers_text = "\n\n".join(
        f"[{p['arxiv_id']}] {p['title']}\nAuthors: {', '.join(p['authors'][:3])}\nAbstract: {p['abstract']}"
        for p in papers
    )

    req = CompletionRequest(
        model="",
        messages=[
            Message(role="system", content=_SYNTHESIS_SYSTEM),
            Message(role="user", content=_SYNTHESIS_USER.format(
                query=query,
                papers_text=papers_text[:6000],
            )),
        ],
        temperature=0.2,
        max_tokens=800,
    )
    raw = run_with_fallback(req)
    text = raw.text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    import json
    try:
        synthesis_data = json.loads(text)
    except Exception:
        synthesis_data = {
            "synthesis": text[:400],
            "key_themes": [],
            "seminal_papers": [],
            "open_questions": [],
            "suggested_follow_ups": [],
        }

    return {
        "query": query,
        "total_found": len(papers),
        "papers": papers,
        **synthesis_data,
        "billing_units_actual": len(papers),
    }
