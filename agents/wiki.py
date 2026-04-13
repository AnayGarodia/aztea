"""
agent_wiki.py — Wikipedia research agent

Input:  { "topic": "..." }
Output: { "title": str, "url": str, "summary": str,
          "key_facts": [str], "related_topics": [str],
          "content_type": "person|place|organization|concept|event|technology|other" }
"""

import json
import re

import groq as _groq
import requests
from groq import Groq

_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama3-70b-8192",
    "mixtral-8x7b-32768",
    "llama-3.1-8b-instant",
]

_WIKI_HEADERS = {"User-Agent": "agentmarket/1.0 (research-agent@agentmarket.dev)"}
_WIKI_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"

_SYSTEM = (
    "You are a research synthesizer. Extract structured, factual intelligence "
    "from Wikipedia content. Return only valid JSON — no markdown, no preamble."
)

_USER = """\
Based on this Wikipedia article, return a JSON object with exactly these fields:
{{
  "title": "official article title",
  "url": "{url}",
  "summary": "3-4 sentence plain-English summary of the topic",
  "key_facts": ["5-8 specific, notable, concrete facts from the article"],
  "related_topics": ["4-6 closely related topics worth exploring"],
  "content_type": "person|place|organization|concept|event|technology|other"
}}

Article content:
---
{content}
---
"""

_MAX_CONTENT_CHARS = 8_000


def run(topic: str) -> dict:
    """
    Fetch the Wikipedia summary for `topic`, then synthesize a structured brief.
    Raises ValueError if the topic is not found or the API call fails.
    """
    clean = topic.strip().replace(" ", "_")
    url = _WIKI_API.format(title=clean)
    try:
        r = requests.get(url, headers=_WIKI_HEADERS, timeout=10)
        r.raise_for_status()
        wiki = r.json()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise ValueError(
                f"Wikipedia article not found for: {topic!r}. "
                "Try a more specific topic name."
            ) from e
        raise ValueError(f"Wikipedia API error: {e}") from e
    except requests.RequestException as e:
        raise ValueError(f"Failed to fetch Wikipedia data: {e}") from e

    extract = wiki.get("extract", "").strip()
    if not extract:
        raise ValueError(f"No content available for topic: {topic!r}")

    page_url = (
        wiki.get("content_urls", {}).get("desktop", {}).get("page", url)
    )

    client = Groq()
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _USER.format(
                url=page_url,
                content=extract[:_MAX_CONTENT_CHARS],
            ),
        },
    ]
    last_err = None
    for model in _MODELS:
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=800, messages=messages
            )
        except _groq.RateLimitError as e:
            last_err = e
            continue
        raw = _strip_fences(resp.choices[0].message.content.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Model {model} returned non-JSON: {e}\n\n{raw[:300]}"
            ) from e
    raise last_err


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return m.group(1).strip() if m else text
