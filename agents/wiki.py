"""
agent_wiki.py — grounded Wikipedia research synthesis.
"""

from __future__ import annotations

import json
import re
from typing import Any

import requests

from agents._contracts import annotate_success
from core.llm import CompletionRequest, Message, run_with_fallback

_WIKI_HEADERS = {"User-Agent": "aztea/1.0 (research-agent@aztea.dev)"}
_WIKI_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_WIKI_SECTIONS_API = "https://en.wikipedia.org/w/api.php"

_SYSTEM = """\
You are a research librarian and fact-synthesis expert trained in encyclopedic analysis.
Use only the article content provided. Keep the output concrete, dense, and grounded.
Return only valid JSON.
"""

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
  "summary": "4-5 sentence dense summary grounded in the article",
  "key_facts": ["specific, concrete facts from the article"],
  "timeline": [{{"date": "year or date range", "event": "specific event"}}],
  "notable_figures": [{{"name": "person name", "role": "specific relationship to topic"}}],
  "statistics": [{{"stat": "specific numerical fact", "source_note": "short provenance note"}}],
  "controversies_and_debates": [{{"topic": "contested issue", "positions": "main disagreement"}}],
  "related_topics": ["closely connected topics for further research"],
  "primary_sources": ["key references or cited documents worth following up"],
  "knowledge_gaps": ["important aspects not covered well by the article"]
}}
"""

_MAX_CONTENT_CHARS = 10_000
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2}|2100)\b")


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return m.group(1).strip() if m else text


def _sentences(text: str) -> list[str]:
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", collapsed) if part.strip()]


def _dedupe(items: list[str], *, limit: int) -> list[str]:
    unique: list[str] = []
    for item in items:
        normalized = re.sub(r"\s+", " ", str(item or "")).strip(" -")
        if normalized and normalized not in unique:
            unique.append(normalized)
        if len(unique) >= limit:
            break
    return unique


def _classify_content_type(topic: str, content: str) -> str:
    lowered = f"{topic} {content[:1000]}".lower()
    if any(token in lowered for token in ("born", "died", "actor", "scientist", "politician", "writer")):
        return "person"
    if any(token in lowered for token in ("company", "corporation", "founded", "subsidiary", "headquartered")):
        return "organization"
    if any(token in lowered for token in ("city", "country", "province", "river", "mountain", "population")):
        return "place"
    if any(token in lowered for token in ("war", "battle", "treaty", "revolution", "election")):
        return "event"
    if any(token in lowered for token in ("algorithm", "software", "protocol", "device", "machine learning")):
        return "technology"
    if any(token in lowered for token in ("theory", "physics", "chemistry", "biology", "mathematics")):
        return "science"
    if any(token in lowered for token in ("film", "album", "novel", "art", "music")):
        return "culture"
    if any(token in lowered for token in ("method", "framework", "concept", "philosophy", "economics")):
        return "concept"
    return "other"


def _deterministic_brief(*, page_title: str, page_url: str, content: str) -> dict[str, Any]:
    sentences = _sentences(content)
    summary = " ".join(sentences[:4]).strip()[:900]
    key_facts = _dedupe(
        [sentence for sentence in sentences if any(char.isdigit() for char in sentence)][:8]
        or sentences[:6],
        limit=8,
    )
    timeline = []
    for sentence in sentences:
        years = _YEAR_RE.findall(sentence)
        if years:
            timeline.append({"date": years[0], "event": sentence[:220]})
        if len(timeline) >= 6:
            break
    statistics = []
    for sentence in sentences:
        if any(char.isdigit() for char in sentence):
            statistics.append({"stat": sentence[:220], "source_note": "From the fetched Wikipedia article text."})
        if len(statistics) >= 4:
            break
    notable_figures = []
    name_matches = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", content[:4000])
    for name in name_matches:
        if name == page_title:
            continue
        notable_figures.append({"name": name, "role": "Named in the fetched article text."})
        if len(notable_figures) >= 5:
            break
    return annotate_success(
        {
            "title": page_title,
            "url": page_url,
            "content_type": _classify_content_type(page_title, content),
            "summary": summary or "Wikipedia content fetched, but synthesis is unavailable.",
            "key_facts": key_facts,
            "timeline": timeline,
            "notable_figures": notable_figures,
            "statistics": statistics,
            "controversies_and_debates": [],
            "related_topics": [],
            "primary_sources": [],
            "knowledge_gaps": [],
        },
        billing_units_actual=1,
        llm_used=False,
        degraded_mode=True,
    )


def _fetch_full_text(title: str) -> tuple[str, str]:
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


def _normalize_llm_output(raw: Any, *, page_title: str, page_url: str, content: str) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    deterministic = _deterministic_brief(page_title=page_title, page_url=page_url, content=content)

    def _string_list(value: Any, fallback: list[str], limit: int) -> list[str]:
        if not isinstance(value, list):
            return fallback[:limit]
        return _dedupe([str(item) for item in value], limit=limit) or fallback[:limit]

    timeline_raw = payload.get("timeline")
    timeline = []
    if isinstance(timeline_raw, list):
        for item in timeline_raw:
            if not isinstance(item, dict):
                continue
            date = str(item.get("date") or "").strip()
            event = str(item.get("event") or "").strip()
            if date and event:
                timeline.append({"date": date[:40], "event": event[:220]})
            if len(timeline) >= 8:
                break
    if not timeline:
        timeline = deterministic["timeline"]

    figures_raw = payload.get("notable_figures")
    figures = []
    if isinstance(figures_raw, list):
        for item in figures_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            role = str(item.get("role") or "").strip()
            if name and role:
                figures.append({"name": name[:120], "role": role[:220]})
            if len(figures) >= 8:
                break
    if not figures:
        figures = deterministic["notable_figures"]

    stats_raw = payload.get("statistics")
    stats = []
    if isinstance(stats_raw, list):
        for item in stats_raw:
            if not isinstance(item, dict):
                continue
            stat = str(item.get("stat") or "").strip()
            source_note = str(item.get("source_note") or "").strip()
            if stat:
                stats.append({"stat": stat[:220], "source_note": (source_note or "From the fetched article text.")[:180]})
            if len(stats) >= 6:
                break
    if not stats:
        stats = deterministic["statistics"]

    return annotate_success(
        {
            "title": str(payload.get("title") or page_title).strip() or page_title,
            "url": page_url,
            "content_type": str(payload.get("content_type") or deterministic["content_type"]).strip() or "other",
            "summary": str(payload.get("summary") or deterministic["summary"]).strip()[:1000],
            "key_facts": _string_list(payload.get("key_facts"), deterministic["key_facts"], 10),
            "timeline": timeline,
            "notable_figures": figures,
            "statistics": stats,
            "controversies_and_debates": [
                {
                    "topic": str(item.get("topic") or "").strip()[:180],
                    "positions": str(item.get("positions") or "").strip()[:280],
                }
                for item in payload.get("controversies_and_debates", [])
                if isinstance(item, dict) and str(item.get("topic") or "").strip()
            ][:5] if isinstance(payload.get("controversies_and_debates"), list) else [],
            "related_topics": _string_list(payload.get("related_topics"), deterministic["related_topics"], 8),
            "primary_sources": _string_list(payload.get("primary_sources"), deterministic["primary_sources"], 8),
            "knowledge_gaps": _string_list(payload.get("knowledge_gaps"), deterministic["knowledge_gaps"], 6),
        },
        billing_units_actual=1,
        llm_used=True,
        degraded_mode=False,
    )


def run(topic: str, depth: str = "standard") -> dict:
    clean = topic.strip().replace(" ", "_")
    summary_url = _WIKI_SUMMARY_API.format(title=clean)

    try:
        r = requests.get(summary_url, headers=_WIKI_HEADERS, timeout=10)
        r.raise_for_status()
        wiki = r.json()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise ValueError(f"Wikipedia article not found for: {topic!r}. Try a more specific name.") from e
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
        resp = run_with_fallback(
            CompletionRequest(
                model="",
                messages=[
                    Message("system", _SYSTEM),
                    Message("user", _USER.format(url=page_url, content=content[:_MAX_CONTENT_CHARS])),
                ],
                max_tokens=1400,
                json_mode=True,
            )
        )
        raw = json.loads(_strip_fences(resp.text))
    except Exception:
        return _deterministic_brief(page_title=page_title, page_url=page_url, content=content)
    return _normalize_llm_output(raw, page_title=page_title, page_url=page_url, content=content)
