"""
agent_textintel.py — Text intelligence agent

Input:  { "text": "...", "mode": "full" }
        mode: full | quick
Output: { "word_count": int, "reading_time_seconds": int, "language": str,
          "sentiment": "positive|negative|neutral|mixed",
          "sentiment_score": float (-1.0 to 1.0),
          "summary": str, "key_entities": [str],
          "main_topics": [str], "key_quotes": [str] }
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = (
    "You are an expert text analyst specializing in NLP, named entity recognition, "
    "and sentiment analysis. Return only valid JSON — no markdown, no preamble."
)

_USER_FULL = """\
Analyze this text and return a JSON object with exactly these fields:
{{
  "word_count": integer,
  "reading_time_seconds": integer (estimate at 200 words/minute),
  "language": "detected ISO language code (en, es, fr, zh, etc)",
  "sentiment": "positive|negative|neutral|mixed",
  "sentiment_score": float from -1.0 (very negative) to 1.0 (very positive),
  "summary": "2-3 sentence plain-English summary",
  "key_entities": ["notable people, organizations, places, or products mentioned"],
  "main_topics": ["3-6 main themes or subjects"],
  "key_quotes": ["1-3 verbatim notable sentences from the text"]
}}

Text:
---
{text}
---
"""

_USER_QUICK = """\
Quickly analyze this text. Return a JSON object:
{{
  "word_count": integer,
  "language": "ISO code",
  "sentiment": "positive|negative|neutral|mixed",
  "sentiment_score": float -1.0 to 1.0,
  "summary": "1-2 sentence summary",
  "main_topics": ["2-4 topics"]
}}

Text:
---
{text}
---
"""

_MAX_TEXT_CHARS = 10_000


def run(text: str, mode: str = "full") -> dict:
    """
    Analyze the provided text. Returns structured intelligence dict.
    mode='quick' uses a shorter prompt and smaller max_tokens.
    """
    word_count = len(text.split())
    template = _USER_FULL if mode == "full" else _USER_QUICK
    max_tokens = 800 if mode == "full" else 400
    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[
            Message("system", _SYSTEM),
            Message("user", template.format(text=text[:_MAX_TEXT_CHARS])),
        ],
        max_tokens=max_tokens,
        json_mode=True,
    ))
    raw = _strip_fences(resp.text)
    try:
        result = json.loads(raw)
        result["word_count"] = word_count
        return result
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned non-JSON: {e}\n\n{raw[:300]}") from e


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return m.group(1).strip() if m else text
