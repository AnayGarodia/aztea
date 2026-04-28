"""
agent_wiki.py — Deep research synthesis agent (Wikipedia-backed)

Input:  { "topic": "...", "depth": "standard|deep" }
Output: {
  "title": str, "url": str, "content_type": str,
  "summary": str,
  "key_facts": [str],
  "timeline": [{"date": str, "event": str}],
  "notable_figures": [{"name": str, "role": str}],
  "statistics": [{"stat": str, "source_note": str}],
  "controversies_and_debates": [{"topic": str, "positions": str}],
  "related_topics": [str],
  "primary_sources": [str],
  "knowledge_gaps": [str]
}
"""

import json
import re

import requests

from core.llm import CompletionRequest, Message, run_with_fallback

_WIKI_HEADERS = {"User-Agent": "aztea/1.0 (research-agent@aztea.dev)"}
_WIKI_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_WIKI_SECTIONS_API = "https://en.wikipedia.org/w/api.php"

_SYSTEM = """\
You are a research librarian and fact-synthesis expert trained in encyclopedic analysis. \
Your job is to extract structured, high-density intelligence from Wikipedia content — the kind \
that saves a researcher 2 hours of reading.

You surface:
- Concrete, specific facts (numbers, dates, names) not vague summaries
- Chronological timelines when a topic has historical depth
- Notable figures and their specific roles/contributions
- Statistics with their original source context
- Genuine controversies, contested claims, or ongoing debates — Wikipedia often buries these
- Gaps in knowledge — what Wikipedia does NOT cover that a researcher would want to know
- Primary sources cited in the article worth following up

Do not pad with obvious information. Be dense and specific.
Return only valid JSON — no markdown, no prose outside the JSON object."""

_USER = """\
Based on this Wikipedia article content, produce a comprehensive research brief.

Article URL: {url}
Content:
---
{content}
---

Return EXACTLY this JSON:
{{
  "title": "official Wikipedia article title",
  "url": "{url}",
  "content_type": "person|place|organization|concept|event|technology|science|culture|other",
  "summary": "4–5 sentence dense summary — include key numbers, dates, and significance",
  "key_facts": ["8–12 specific, concrete, notable facts — include numbers and dates where possible"],
  "timeline": [
    {{
      "date": "year or date range",
      "event": "specific event or development"
    }}
  ],
  "notable_figures": [
    {{
      "name": "person name",
      "role": "specific contribution or relationship to the topic"
    }}
  ],
  "statistics": [
    {{
      "stat": "specific numerical fact",
      "source_note": "where this stat comes from (as cited in Wikipedia)"
    }}
  ],
  "controversies_and_debates": [
    {{
      "topic": "contested issue",
      "positions": "summary of the main disagreement — who says what"
    }}
  ],
  "related_topics": ["5–8 closely connected topics for further research — be specific"],
  "primary_sources": ["key references, reports, or documents cited in the article worth consulting"],
  "knowledge_gaps": ["what important aspects are NOT covered in this Wikipedia article"]
}}
"""

_MAX_CONTENT_CHARS = 10_000


def _fallback_brief(*, page_title: str, page_url: str, content: str) -> dict:
    summary = content.strip()[:600]
    return {
        "title": page_title,
        "url": page_url,
        "content_type": "other",
        "summary": summary or "Wikipedia content fetched, but synthesis is unavailable.",
        "key_facts": [],
        "timeline": [],
        "notable_figures": [],
        "statistics": [],
        "controversies_and_debates": [],
        "related_topics": [],
        "primary_sources": [],
        "knowledge_gaps": [],
    }


def _fetch_full_text(title: str) -> tuple[str, str]:
    """Fetch article text via MediaWiki API (richer than REST summary)."""
    params = {
        "action": "query",
        "titles": title,
        "prop": "extracts",
        "exintro": False,
        "explaintext": True,
        "exsectionformat": "plain",
        "format": "json",
        "utf8": 1,
    }
    try:
        r = requests.get(_WIKI_SECTIONS_API, params=params, headers=_WIKI_HEADERS, timeout=12)
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        return page.get("extract", ""), page.get("title", title)
    except Exception:
        return "", title


def run(topic: str, depth: str = "standard") -> dict:
    clean = topic.strip().replace(" ", "_")
    summary_url = _WIKI_SUMMARY_API.format(title=clean)

    try:
        r = requests.get(summary_url, headers=_WIKI_HEADERS, timeout=10)
        r.raise_for_status()
        wiki = r.json()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise ValueError(
                f"Wikipedia article not found for: {topic!r}. Try a more specific name."
            ) from e
        raise ValueError(f"Wikipedia API error: {e}") from e
    except requests.RequestException as e:
        raise ValueError(f"Failed to fetch Wikipedia data: {e}") from e

    page_url = wiki.get("content_urls", {}).get("desktop", {}).get("page", summary_url)
    page_title = wiki.get("title", topic)

    if depth == "deep":
        full_text, page_title = _fetch_full_text(page_title)
        content = full_text if full_text else (wiki.get("extract", "") or "")
    else:
        content = wiki.get("extract", "") or ""

    if not content.strip():
        raise ValueError(f"No content available for topic: {topic!r}")

    try:
        resp = run_with_fallback(CompletionRequest(
            model="",
            messages=[
                Message("system", _SYSTEM),
                Message("user", _USER.format(url=page_url, content=content[:_MAX_CONTENT_CHARS])),
            ],
            max_tokens=1400,
            json_mode=True,
        ))
        raw = _strip_fences(resp.text)
    except Exception:
        return _fallback_brief(page_title=page_title, page_url=page_url, content=content)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return _fallback_brief(page_title=page_title, page_url=page_url, content=content)


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return m.group(1).strip() if m else text
