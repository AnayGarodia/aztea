"""First chunk of built-in agent specs (initial `specs = [...]` list)."""
from __future__ import annotations

from typing import Any

from core.models import (
    CodeReviewRequest,
    FinancialRequest,
    WikiRequest,
)
from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
    CODEREVIEW_AGENT_ID as _CODEREVIEW_AGENT_ID,
    CVELOOKUP_AGENT_ID as _CVELOOKUP_AGENT_ID,
    FINANCIAL_AGENT_ID as _FINANCIAL_AGENT_ID,
    QUALITY_JUDGE_AGENT_ID as _QUALITY_JUDGE_AGENT_ID,
    WIKI_AGENT_ID as _WIKI_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object
from server.builtin_agents.schemas import quality_judge_input_schema as _quality_judge_input_schema


def load_builtin_specs_part1() -> list[dict[str, Any]]:
    return [
{
    "agent_id": _FINANCIAL_AGENT_ID,
    "name": "Financial Research Agent",
    "description": "Use when looking up financial data for a public company. Fetches the latest 10-K or 10-Q directly from SEC EDGAR — not stale LLM memory. Returns a structured brief: business summary, financial highlights, key risks, and a buy/hold/sell signal with reasoning.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_FINANCIAL_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["financial-research", "sec-filings", "equity-analysis"],
    "input_schema": FinancialRequest.model_json_schema(),
    "output_schema": _output_schema_object(
        {
            "ticker": {"type": "string"},
            "company_name": {"type": "string"},
            "filing_type": {"type": "string"},
            "filing_date": {"type": "string"},
            "business_summary": {"type": "string"},
            "recent_financial_highlights": {"type": "array", "items": {"type": "string"}},
            "key_risks": {"type": "array", "items": {"type": "string"}},
            "signal": {"type": "string"},
            "signal_reasoning": {"type": "string"},
            "generated_at": {"type": "string"},
        },
        required=["ticker", "signal"],
    ),
    "output_examples": [
        {
            "input": {"ticker": "AAPL"},
            "output": {
                "ticker": "AAPL",
                "company_name": "Apple Inc.",
                "filing_type": "10-Q",
                "filing_date": "2026-01-31",
                "business_summary": "Consumer hardware and services ecosystem.",
                "recent_financial_highlights": ["Revenue growth in Services", "Stable gross margin"],
                "key_risks": ["Regulatory pressure", "Supply chain concentration"],
                "signal": "positive",
                "signal_reasoning": "Recurring revenue expansion offsets hardware cyclicality.",
                "generated_at": "2026-02-01T00:00:00+00:00",
            },
        },
        {
            "input": {"ticker": "TSLA"},
            "output": {
                "ticker": "TSLA",
                "company_name": "Tesla, Inc.",
                "filing_type": "10-Q",
                "filing_date": "2026-02-05",
                "business_summary": "EV manufacturing and energy storage business.",
                "recent_financial_highlights": ["Automotive margin compression", "Energy growth"],
                "key_risks": ["Price competition", "Execution risk on new models"],
                "signal": "neutral",
                "signal_reasoning": "Growth opportunities remain, but profitability volatility is elevated.",
                "generated_at": "2026-02-06T00:00:00+00:00",
            },
        },
    ],
},
{
    "agent_id": _CODEREVIEW_AGENT_ID,
    "name": "Code Review Agent",
    "description": "Use when the user wants a dedicated code review pass rather than an inline suggestion. Runs a structured analysis against OWASP Top 10 and returns CWE IDs, CVSS-ranked vulnerabilities, a complexity score, and copy-paste-ready fixes for each issue.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_CODEREVIEW_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["code-review", "security", "developer-tools"],
    "input_schema": CodeReviewRequest.model_json_schema(),
    "output_schema": _output_schema_object(
        {
            "language_detected": {"type": "string"},
            "score": {"type": "integer"},
            "issues": {"type": "array", "items": {"type": "object"}},
            "positive_aspects": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
        },
        required=["score", "summary"],
    ),
    "output_examples": [
        {
            "input": {
                "code": "def divide(a, b):\n    return a / b\n",
                "language": "python",
                "focus": "bugs",
            },
            "output": {
                "language_detected": "python",
                "score": 78,
                "issues": [
                    {
                        "severity": "medium",
                        "title": "Missing zero-division guard",
                        "suggestion": "Handle b == 0 before division.",
                    }
                ],
                "positive_aspects": ["Function is concise and readable."],
                "summary": "Core logic is correct but missing input safety checks.",
            },
        },
        {
            "input": {
                "code": "const token = req.headers.authorization;\nconsole.log(token);",
                "language": "javascript",
                "focus": "security",
            },
            "output": {
                "language_detected": "javascript",
                "score": 62,
                "issues": [
                    {
                        "severity": "high",
                        "title": "Sensitive token logging",
                        "suggestion": "Remove token logging or redact before logging.",
                    }
                ],
                "positive_aspects": ["Simple extraction flow."],
                "summary": "Avoid exposing secrets in logs.",
            },
        },
    ],
},
{
    "agent_id": _WIKI_AGENT_ID,
    "name": "Wikipedia Research Agent",
    "description": "Use when the task requires structured Wikipedia research with more depth than a quick summary. Fetches live article content and returns key facts, timelines, notable figures, statistics with citations, and follow-up sources.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_WIKI_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["research", "knowledge-base", "wikipedia"],
    "input_schema": WikiRequest.model_json_schema(),
    "output_schema": _output_schema_object(
        {
            "title": {"type": "string"},
            "url": {"type": "string"},
            "summary": {"type": "string"},
            "key_facts": {"type": "array", "items": {"type": "string"}},
            "related_topics": {"type": "array", "items": {"type": "string"}},
            "content_type": {"type": "string"},
        },
        required=["title", "summary"],
    ),
    "output_examples": [
        {
            "input": {"topic": "Discounted cash flow"},
            "output": {
                "title": "Discounted cash flow",
                "url": "https://en.wikipedia.org/wiki/Discounted_cash_flow",
                "summary": "Valuation method based on present value of expected future cash flows.",
                "key_facts": [
                    "Uses a discount rate to reflect risk and time value.",
                    "Common in equity and project valuation.",
                ],
                "related_topics": ["Net present value", "Weighted average cost of capital"],
                "content_type": "encyclopedia_article",
            },
        },
        {
            "input": {"topic": "Porter's five forces"},
            "output": {
                "title": "Porter's five forces analysis",
                "url": "https://en.wikipedia.org/wiki/Porter%27s_five_forces_analysis",
                "summary": "Framework for analyzing competition and profitability drivers in an industry.",
                "key_facts": ["Covers supplier power, buyer power, rivalry, substitutes, and entrants."],
                "related_topics": ["Competitive strategy", "Industry analysis"],
                "content_type": "encyclopedia_article",
            },
        },
    ],
},
{
    "agent_id": _QUALITY_JUDGE_AGENT_ID,
    "name": "Quality Judge Agent",
    "description": "Internal verification worker that scores completed outputs before settlement.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_QUALITY_JUDGE_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["quality", "internal"],
    "input_schema": _quality_judge_input_schema(),
    "output_schema": _output_schema_object(
        {
            "verdict": {"type": "string"},
            "score": {"type": "integer"},
            "reason": {"type": "string"},
        },
        required=["verdict", "score", "reason"],
    ),
    "output_examples": [
        {
            "input": {
                "input_payload": {"task": "Summarize filing risks"},
                "output_payload": {"summary": "Identified debt covenant and supply-chain risks."},
                "agent_description": "SEC filing analyst",
            },
            "output": {
                "verdict": "pass",
                "score": 86,
                "reason": "Output is relevant, structured, and addresses requested risk focus.",
            },
        },
        {
            "input": {
                "input_payload": {"task": "Provide concise bug report"},
                "output_payload": {"text": "Looks good."},
                "agent_description": "Code review specialist",
            },
            "output": {
                "verdict": "fail",
                "score": 22,
                "reason": "Response is too generic and lacks actionable findings.",
            },
        },
    ],
    "internal_only": True,
},
{
    "agent_id": _CVELOOKUP_AGENT_ID,
    "name": "CVE Lookup Agent",
    "description": "Use when the user wants live CVE data for a package or specific CVE ID. Queries NIST NVD in real time — not LLM memory. Returns CVSS score, exploit availability, affected version range, and recommended fix for each CVE.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_CVELOOKUP_AGENT_ID],
    "price_per_call_usd": 0.06,
    "tags": ["security", "cve", "vulnerability-intel", "nvd", "packages"],
    "input_schema": {
        "type": "object",
        "properties": {
            "cve_id": {"type": "string", "description": "A single CVE ID to look up directly (e.g. CVE-2021-44228)"},
            "cve_ids": {"type": "array", "items": {"type": "string"}, "description": "Multiple CVE IDs to look up (max 10)"},
            "packages": {"type": "array", "items": {"type": "string"}, "description": "Array of package@version strings", "example": ["express@4.17.1", "lodash@4.17.20"]},
            "include_patched": {"type": "boolean", "default": False},
        },
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "results": {"type": "array", "items": {"type": "object"}},
            "billing_units_actual": {"type": "integer", "description": "Number of successful CVE lookups (for per-CVE billing in direct ID mode)"},
            "total_vulnerable": {"type": "integer"},
            "summary": {"type": "string"},
        },
    },
    "variable_pricing": {
        "model": "tiered",
        "field": "cve_ids",
        "field_type": "array",
        "unit_label": "CVE",
        "tiers": [
            {"max_units": 1,  "price_usd": 0.01},
            {"max_units": 5,  "price_usd": 0.03},
            {"max_units": 10, "price_usd": 0.06},
        ],
    },
    "output_examples": [
        {
            "input": {"packages": ["lodash@4.17.20", "express@4.17.1"]},
            "output": {
                "results": [{"package": "lodash", "version": "4.17.20", "cve": "CVE-2019-10744", "cvss": 9.1, "severity": "critical"}],
                "total_vulnerable": 2,
                "total_packages_checked": 2,
                "summary": "lodash@4.17.20 has 2 known CVEs including CVE-2019-10744 (prototype pollution, CVSS 9.1).",
            },
        },
    ],
},
    ]
