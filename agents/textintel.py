"""
agent_textintel.py — Deep text intelligence agent

Input:  { "text": "...", "mode": "full|quick|claims|rhetoric" }
Output (full): {
  "word_count": int, "reading_time_seconds": int, "language": str,
  "reading_level": str,                # e.g. "Grade 12 / College"
  "sentiment": str, "sentiment_score": float,
  "objectivity_score": float,          # 0.0 = pure opinion, 1.0 = purely factual
  "summary": str,
  "key_entities": [{"name": str, "type": str, "role": str}],
  "main_topics": [str],
  "claims": [{"claim": str, "verifiable": bool, "confidence": str}],
  "logical_fallacies": [{"type": str, "excerpt": str, "explanation": str}],
  "rhetorical_devices": [str],
  "emotional_tone": [str],
  "key_quotes": [str],
  "bias_indicators": [str]
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a computational linguist and investigative journalist with expertise in \
discourse analysis, rhetoric, cognitive bias detection, and argumentation theory. \
You have trained on thousands of persuasive texts, scientific papers, news articles, \
and legal documents. Your analysis surfaces what most readers miss.

You identify:
- Named entities with their semantic role (protagonist, antagonist, source, subject)
- Verifiable factual claims vs. opinions vs. normative judgments
- Classical logical fallacies: ad hominem, straw man, appeal to authority, false dichotomy, etc.
- Rhetorical devices: anaphora, epistrophe, hyperbole, ethos/pathos/logos appeals, hedging language
- Bias indicators: selection bias, framing effects, loaded language, omission signals
- Reading level using Flesch-Kincaid grade equivalents

Return only valid JSON — no markdown, no prose outside the JSON object."""

_USER_FULL = """\
Perform a deep linguistic and rhetorical analysis of this text. Return a JSON object:
{{
  "word_count": integer,
  "reading_time_seconds": integer (at 238 words/minute average),
  "language": "ISO 639-1 code",
  "reading_level": "e.g. Grade 8 / Middle School or College / Graduate",
  "sentiment": "positive|negative|neutral|mixed",
  "sentiment_score": float from -1.0 (strongly negative) to 1.0 (strongly positive),
  "objectivity_score": float from 0.0 (pure opinion/rhetoric) to 1.0 (purely factual),
  "summary": "3–4 sentence plain-English summary capturing the core message and intent",
  "key_entities": [
    {{
      "name": "entity name",
      "type": "person|organization|place|product|concept|event|date|statistic",
      "role": "how this entity functions in the text (e.g. protagonist, cited authority, subject of criticism)"
    }}
  ],
  "main_topics": ["3–6 concrete themes — be specific, not generic"],
  "claims": [
    {{
      "claim": "specific factual assertion made in the text",
      "verifiable": true or false,
      "confidence": "high|medium|low (how confidently it is stated)"
    }}
  ],
  "logical_fallacies": [
    {{
      "type": "fallacy name (e.g. Appeal to Authority, Straw Man, False Dichotomy)",
      "excerpt": "verbatim text that exhibits the fallacy",
      "explanation": "why this is a fallacy in this context"
    }}
  ],
  "rhetorical_devices": ["list of named devices found (e.g. Anaphora, Hyperbole, Ethos appeal)"],
  "emotional_tone": ["specific emotions evoked: fear, urgency, nostalgia, outrage, hope, etc."],
  "key_quotes": ["2–4 verbatim sentences most central to the text's argument"],
  "bias_indicators": ["specific signals of bias, framing, or selective emphasis"]
}}

Text:
---
{text}
---
"""

_USER_QUICK = """\
Quickly analyze this text and return a JSON object:
{{
  "word_count": integer,
  "language": "ISO code",
  "sentiment": "positive|negative|neutral|mixed",
  "sentiment_score": float -1.0 to 1.0,
  "objectivity_score": float 0.0 to 1.0,
  "summary": "2-sentence summary",
  "main_topics": ["2–4 topics"],
  "key_entities": ["named people, orgs, or places"]
}}

Text:
---
{text}
---
"""

_USER_CLAIMS = """\
Extract all factual claims from this text. Return a JSON object:
{{
  "total_claims": integer,
  "claims": [
    {{
      "claim": "verbatim or near-verbatim claim from text",
      "type": "factual|statistical|historical|predictive|normative",
      "verifiable": true or false,
      "confidence_level": "high|medium|low (as stated by the author)",
      "source_cited": true or false
    }}
  ],
  "unsourced_high_confidence_claims": ["claims stated as certain but with no cited source"]
}}

Text:
---
{text}
---
"""

_USER_RHETORIC = """\
Analyze the rhetorical structure and persuasion techniques in this text. Return JSON:
{{
  "persuasion_mode": "ethos|pathos|logos|mixed",
  "rhetorical_devices": [
    {{
      "device": "device name",
      "excerpt": "verbatim example from text",
      "effect": "what effect this creates in the reader"
    }}
  ],
  "logical_fallacies": [
    {{
      "type": "fallacy name",
      "excerpt": "verbatim excerpt",
      "explanation": "why it is a fallacy here"
    }}
  ],
  "bias_indicators": [
    {{
      "type": "type of bias (framing, selection, anchoring, etc.)",
      "evidence": "specific text or pattern that signals this bias"
    }}
  ],
  "overall_assessment": "2-3 sentence verdict on the text's rhetorical integrity"
}}

Text:
---
{text}
---
"""

_MAX_TEXT_CHARS = 12_000


def run(text: str, mode: str = "full") -> dict:
    word_count = len(text.split())
    if mode == "quick":
        template = _USER_QUICK
        max_tokens = 500
    elif mode == "claims":
        template = _USER_CLAIMS
        max_tokens = 900
    elif mode == "rhetoric":
        template = _USER_RHETORIC
        max_tokens = 1000
    else:
        template = _USER_FULL
        max_tokens = 1400

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
