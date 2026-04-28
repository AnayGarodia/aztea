"""
hn_digest.py — Fetch and synthesize top Hacker News front-page stories

Input:  {
  "count": 10,              # number of stories, 1-20 (default 10)
  "topic_filter": "",       # optional; filter by title substring (case-insensitive)
  "include_comments": false,# reserved flag (not yet implemented - requires HN Firebase API)
  "mode": "digest"          # "digest" | "trends" | "hot"
}
Output: {
  "stories": [...],
  "synthesis": str,
  "trending_topics": [str],
  "notable_discussions": [str],
  "billing_units_actual": int
}
"""

import httpx

from core.llm import CompletionRequest, Message, run_with_fallback
from agents._contracts import annotate_success

_HN_ALGOLIA = "https://hn.algolia.com/api/v1/search"
_TIMEOUT = 10


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}

_SYSTEM = """\
You are a sharp technology analyst who reads Hacker News daily.
Given a list of front-page stories (title, score, comment count), produce
a crisp synthesis based on the requested mode.
Return only plain text — no markdown headers, no bullet symbols."""

_USER_DIGEST = """\
Mode: digest — synthesize the 3-5 dominant themes on today's front page.

Stories:
{stories_text}

Provide a 3-5 sentence synthesis of the major themes. Then on a new line
write "TOPICS:" followed by 3-5 comma-separated single-word or short-phrase
topic labels that capture the themes."""

_USER_TRENDS = """\
Mode: trends — identify which stories signal genuine shifts vs noise.

Stories:
{stories_text}

Identify the 2-3 stories or patterns that signal real inflection points rather
than one-day noise. Explain briefly. Then on a new line write "TOPICS:"
followed by 3-5 comma-separated trend labels."""

_USER_HOT = """\
Mode: hot — flag stories with unusually high comment-to-score ratio (heated debates).

Stories:
{stories_text}

Identify which stories are generating the most debate relative to their score.
Explain the apparent controversy. Then on a new line write "TOPICS:"
followed by 3-5 comma-separated labels for the debate themes."""


def _build_prompt(mode: str, stories_text: str) -> str:
    if mode == "trends":
        return _USER_TRENDS.format(stories_text=stories_text)
    if mode == "hot":
        return _USER_HOT.format(stories_text=stories_text)
    return _USER_DIGEST.format(stories_text=stories_text)


def _parse_topics(text: str) -> list[str]:
    """Extract topic labels from the TOPICS: line in the LLM output."""
    for line in text.splitlines():
        if line.upper().startswith("TOPICS:"):
            raw = line.split(":", 1)[1].strip()
            topics = [t.strip() for t in raw.split(",") if t.strip()]
            return topics[:5]
    # Fallback: no TOPICS line found
    return []


def run(payload: dict) -> dict:
    """Deprecated agent. Fetches top Hacker News stories and returns an LLM-generated digest.

    Accepts ``count`` (default 10), ``topic_filter``, and ``mode`` (digest/trends/hot).
    """
    try:
        count = max(1, min(int(payload.get("count", 10)), 20))
    except (TypeError, ValueError):
        count = 10

    topic_filter = str(payload.get("topic_filter", "")).strip().lower()
    mode = str(payload.get("mode", "digest")).lower()
    if mode not in ("digest", "trends", "hot"):
        mode = "digest"

    fetch_count = count * 2  # fetch extra to allow filtering
    try:
        resp = httpx.get(
            _HN_ALGOLIA,
            params={"tags": "front_page", "hitsPerPage": fetch_count},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return _err("hn_digest.timeout", "HN Algolia API timed out. Try again in a moment.")
    except httpx.HTTPStatusError as exc:
        return _err("hn_digest.http_error", f"HN Algolia API returned HTTP {exc.response.status_code}")
    except Exception as exc:
        return _err("hn_digest.upstream_unreachable", f"Could not reach HN Algolia API: {type(exc).__name__}")

    hits = data.get("hits", [])

    if topic_filter:
        hits = [h for h in hits if topic_filter in (h.get("title") or "").lower()]

    hits = hits[:count]

    stories = []
    for h in hits:
        stories.append({
            "title": h.get("title") or "",
            "url": h.get("url") or "",
            "score": h.get("points") or 0,
            "comments": h.get("num_comments") or 0,
            "author": h.get("author") or "",
            "age": h.get("created_at") or "",
        })

    if not stories:
        return {
            "stories": [],
            "synthesis": "No stories matched the filter criteria.",
            "trending_topics": [],
            "notable_discussions": [],
            "billing_units_actual": 0,
        }

    stories_text = "\n".join(
        f"{i+1}. [{s['score']} pts / {s['comments']} comments] {s['title']}"
        for i, s in enumerate(stories)
    )

    req = CompletionRequest(
        model="",
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(role="user", content=_build_prompt(mode, stories_text)),
        ],
        temperature=0.3,
        max_tokens=600,
    )
    try:
        raw = run_with_fallback(req)
        synthesis_full = raw.text.strip()
        llm_used = True
        degraded_mode = False
    except Exception:
        synthesis_full = (
            f"Retrieved {len(stories)} Hacker News front-page stories, but synthesis is unavailable "
            "because no LLM provider is configured."
        )
        llm_used = False
        degraded_mode = True

    trending_topics = _parse_topics(synthesis_full)

    # Remove the TOPICS: line from synthesis for cleaner output
    synthesis_lines = [
        line for line in synthesis_full.splitlines()
        if not line.upper().startswith("TOPICS:")
    ]
    synthesis = " ".join(synthesis_lines).strip()

    notable_discussions = [
        s["title"] for s in stories if s["comments"] > 100
    ][:3]

    return annotate_success({
        "stories": stories,
        "synthesis": synthesis,
        "trending_topics": trending_topics,
        "notable_discussions": notable_discussions,
        "billing_units_actual": len(stories),
    }, llm_used=llm_used, degraded_mode=degraded_mode)
