"""
synthesizer.py — grounded SEC filing brief generation.

The synthesis path extracts concrete evidence from the filing first, then asks
the LLM to summarize only that evidence. If the model path is unavailable or
returns malformed output, the agent still returns a deterministic, structured
brief instead of failing open or inventing unsupported claims.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from agents._contracts import annotate_success
from core.llm import CompletionRequest, Message, run_with_fallback
from core.llm.errors import LLMError

BRIEF_SCHEMA = {
    "ticker": "string",
    "company_name": "string",
    "filing_type": "10-K or 10-Q",
    "filing_date": "YYYY-MM-DD",
    "business_summary": "2-3 sentence plain-English description of what the company does",
    "recent_financial_highlights": "JSON array of 3-5 factual bullets grounded in the filing evidence",
    "key_risks": "JSON array of 3-5 factual bullets grounded in the filing evidence",
    "signal": "positive | neutral | negative",
    "signal_reasoning": "1-2 sentence explanation tied to filing evidence only",
    "generated_at": "ISO 8601 timestamp",
}

SYSTEM_PROMPT = """\
You are a senior equity analyst. You read SEC filings and extract structured,
factual investment intelligence. Use only the filing evidence provided to you.
Do not speculate, do not give price targets, and do not invent metrics not
present in the evidence pack.

Return only valid JSON and nothing else.
"""

USER_PROMPT_TEMPLATE = """\
Analyze the following SEC {filing_type} filing evidence for {company_name} ({ticker}),
filed on {filing_date}.

Return a JSON object with exactly these fields:
{schema}

Rules:
- recent_financial_highlights and key_risks must each be a JSON array of strings.
- signal must be exactly one of: "positive", "neutral", "negative".
- generated_at must be the current UTC time in ISO 8601 format.
- Every highlight and risk must be grounded in the evidence below.
- Do not include any text outside the JSON object.

Evidence pack:
Business context:
{business_context}

Financial highlights:
{financial_highlights}

Risk snippets:
{risk_snippets}
"""

_FINANCIAL_KEYWORDS = (
    "revenue",
    "net income",
    "net loss",
    "gross margin",
    "operating margin",
    "operating income",
    "cash and cash equivalents",
    "free cash flow",
    "operating cash flow",
    "liquidity",
    "guidance",
)
_RISK_KEYWORDS = (
    "risk",
    "competition",
    "regulation",
    "supply chain",
    "litigation",
    "cybersecurity",
    "inflation",
    "debt",
    "liquidity",
    "customer concentration",
    "geopolitical",
    "tariff",
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if match:
        return match.group(1).strip()
    return text


def _dedupe(items: list[str], *, limit: int) -> list[str]:
    unique: list[str] = []
    for item in items:
        normalized = re.sub(r"\s+", " ", str(item or "")).strip(" -")
        if normalized and normalized not in unique:
            unique.append(normalized)
        if len(unique) >= limit:
            break
    return unique


def _sentences(text: str) -> list[str]:
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    raw = re.split(r"(?<=[.!?])\s+", collapsed)
    return [part.strip() for part in raw if part.strip()]


def _select_sentences(
    sentences: list[str], keywords: tuple[str, ...], *, limit: int
) -> list[str]:
    matches: list[str] = []
    for sentence in sentences:
        lowered = sentence.lower()
        if any(keyword in lowered for keyword in keywords):
            matches.append(sentence)
    return _dedupe(matches, limit=limit)


def _extract_business_summary(sentences: list[str]) -> str:
    selected = _dedupe(sentences[:3], limit=2)
    summary = " ".join(selected).strip()
    if summary:
        return summary[:450]
    return "Business summary could not be extracted from the filing excerpt."


def _deterministic_signal(highlights: list[str], risks: list[str]) -> tuple[str, str]:
    positive_terms = ("growth", "increased", "improved", "strong", "expanded", "profit")
    negative_terms = (
        "decline",
        "decreased",
        "loss",
        "impairment",
        "pressure",
        "weak",
        "restructuring",
    )
    pos = sum(
        1 for item in highlights if any(term in item.lower() for term in positive_terms)
    )
    neg = sum(
        1
        for item in highlights + risks
        if any(term in item.lower() for term in negative_terms)
    )
    if pos >= neg + 2:
        return (
            "positive",
            "Recent filing evidence skews constructive relative to the highlighted risks.",
        )
    if neg >= pos + 2:
        return (
            "negative",
            "Recent filing evidence is dominated by downside pressure and explicit risk language.",
        )
    return (
        "neutral",
        "The filing presents a mixed picture, so the deterministic fallback stays neutral.",
    )


def _evidence_pack(filing_data: dict[str, Any]) -> dict[str, Any]:
    text = str(filing_data.get("text") or "")
    sentences = _sentences(text)
    business_summary = _extract_business_summary(sentences)
    highlights = _select_sentences(sentences, _FINANCIAL_KEYWORDS, limit=5)
    risks = _select_sentences(sentences, _RISK_KEYWORDS, limit=5)
    signal, signal_reasoning = _deterministic_signal(highlights, risks)
    return {
        "business_summary": business_summary,
        "financial_highlights": highlights,
        "risk_snippets": risks,
        "signal": signal,
        "signal_reasoning": signal_reasoning,
        "source_document_url": str(filing_data.get("document_url") or "").strip(),
    }


def _fallback_brief(
    filing_data: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    return annotate_success(
        {
            "ticker": filing_data["ticker"],
            "company_name": filing_data["company_name"],
            "filing_type": filing_data["filing_type"],
            "filing_date": filing_data["filing_date"],
            "business_summary": evidence["business_summary"],
            "recent_financial_highlights": evidence["financial_highlights"][:5],
            "key_risks": evidence["risk_snippets"][:5],
            "signal": evidence["signal"],
            "signal_reasoning": evidence["signal_reasoning"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_document_url": evidence["source_document_url"],
            "source_evidence": {
                "financial_highlights": evidence["financial_highlights"][:5],
                "risk_snippets": evidence["risk_snippets"][:5],
            },
        },
        billing_units_actual=1,
        llm_used=False,
        degraded_mode=True,
    )


def _normalize_brief_output(
    raw: Any, filing_data: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    highlights = payload.get("recent_financial_highlights")
    risks = payload.get("key_risks")
    signal = str(payload.get("signal") or evidence["signal"]).strip().lower()
    if signal not in {"positive", "neutral", "negative"}:
        signal = evidence["signal"]
    return annotate_success(
        {
            "ticker": filing_data["ticker"],
            "company_name": filing_data["company_name"],
            "filing_type": filing_data["filing_type"],
            "filing_date": filing_data["filing_date"],
            "business_summary": str(
                payload.get("business_summary") or evidence["business_summary"]
            ).strip()[:500],
            "recent_financial_highlights": _dedupe(
                [str(item) for item in highlights]
                if isinstance(highlights, list)
                else evidence["financial_highlights"],
                limit=5,
            ),
            "key_risks": _dedupe(
                [str(item) for item in risks]
                if isinstance(risks, list)
                else evidence["risk_snippets"],
                limit=5,
            ),
            "signal": signal,
            "signal_reasoning": str(
                payload.get("signal_reasoning") or evidence["signal_reasoning"]
            ).strip()[:500],
            "generated_at": str(
                payload.get("generated_at") or datetime.now(timezone.utc).isoformat()
            ).strip(),
            "source_document_url": evidence["source_document_url"],
            "source_evidence": {
                "financial_highlights": evidence["financial_highlights"][:5],
                "risk_snippets": evidence["risk_snippets"][:5],
            },
        },
        billing_units_actual=1,
        llm_used=True,
        degraded_mode=False,
    )


def synthesize_brief(filing_data: dict[str, Any]) -> dict[str, Any]:
    evidence = _evidence_pack(filing_data)
    schema_str = json.dumps(BRIEF_SCHEMA, indent=2)
    user_prompt = USER_PROMPT_TEMPLATE.format(
        filing_type=filing_data["filing_type"],
        company_name=filing_data["company_name"],
        ticker=filing_data["ticker"],
        filing_date=filing_data["filing_date"],
        schema=schema_str,
        business_context=evidence["business_summary"],
        financial_highlights="\n".join(
            f"- {item}" for item in evidence["financial_highlights"]
        )
        or "- No high-confidence financial highlights extracted.",
        risk_snippets="\n".join(f"- {item}" for item in evidence["risk_snippets"])
        or "- No explicit risk snippets extracted from the available filing excerpt.",
    )

    req = CompletionRequest(
        model="",
        messages=[
            Message(role="system", content=SYSTEM_PROMPT),
            Message(role="user", content=user_prompt),
        ],
        temperature=0.1,
        max_tokens=1024,
        json_mode=True,
    )

    try:
        response = run_with_fallback(req)
    except LLMError:
        return _fallback_brief(filing_data, evidence)
    except Exception:
        return _fallback_brief(filing_data, evidence)

    raw = _strip_fences(response.text.strip())

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return _fallback_brief(filing_data, evidence)
    return _normalize_brief_output(parsed, filing_data, evidence)
