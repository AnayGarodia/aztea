"""
synthesizer.py — Groq-powered brief generation

Takes raw SEC filing data and uses Groq to produce a structured
investment brief as a Python dict. All prompt logic lives here.
"""

import json
import re
from groq import Groq

BRIEF_SCHEMA = {
    "ticker": "string",
    "company_name": "string",
    "filing_type": "10-K or 10-Q",
    "filing_date": "YYYY-MM-DD",
    "business_summary": "2-3 sentence plain-English description of what the company does",
    "recent_financial_highlights": "3-5 bullet points on revenue, margins, cash, guidance",
    "key_risks": "3-5 bullet points on the most material risks",
    "signal": "positive | neutral | negative",
    "signal_reasoning": "1-2 sentence explanation of the signal",
    "generated_at": "ISO 8601 timestamp",
}

SYSTEM_PROMPT = """\
You are a senior equity analyst. You read SEC filings and extract structured, \
factual investment intelligence. You never speculate beyond what the filing says. \
You respond only with valid JSON and nothing else — no markdown fences, no preamble.\
"""

USER_PROMPT_TEMPLATE = """\
Analyze the following SEC {filing_type} filing for {company_name} ({ticker}), \
filed on {filing_date}.

Return a JSON object with exactly these fields:
{schema}

Rules:
- recent_financial_highlights and key_risks must each be a JSON array of strings.
- signal must be exactly one of: "positive", "neutral", "negative".
- generated_at must be the current UTC time in ISO 8601 format.
- Do not include any text outside the JSON object.

Filing text (first ~20,000 characters):
---
{filing_text}
---
"""


def synthesize_brief(filing_data: dict) -> dict:
    """
    Call Groq with the filing text and return a parsed investment brief dict.
    Raises ValueError if the response cannot be parsed as JSON.
    """
    client = Groq()

    schema_str = json.dumps(BRIEF_SCHEMA, indent=2)
    user_prompt = USER_PROMPT_TEMPLATE.format(
        filing_type=filing_data["filing_type"],
        company_name=filing_data["company_name"],
        ticker=filing_data["ticker"],
        filing_date=filing_data["filing_date"],
        schema=schema_str,
        filing_text=filing_data["text"],
    )

    completion = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=1024,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw = completion.choices[0].message.content.strip()

    # Strip markdown fences if the model wraps the JSON despite instructions
    raw = _strip_fences(raw)

    try:
        brief = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model returned non-JSON response: {e}\n\nRaw:\n{raw[:500]}") from e

    return brief


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers if present."""
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if match:
        return match.group(1).strip()
    return text
