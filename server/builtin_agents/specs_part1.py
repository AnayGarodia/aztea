"""First chunk of built-in agent specs (initial `specs = [...]` list)."""
from __future__ import annotations

from typing import Any

from core.models import (
    CodeReviewRequest,
    FinancialRequest,
    NegotiationRequest,
    PortfolioRequest,
    ProductStrategyRequest,
    ScenarioRequest,
    TextIntelRequest,
    WikiRequest,
)
from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
    CODEREVIEW_AGENT_ID as _CODEREVIEW_AGENT_ID,
    CVELOOKUP_AGENT_ID as _CVELOOKUP_AGENT_ID,
    DATAINSIGHTS_AGENT_ID as _DATAINSIGHTS_AGENT_ID,
    DEPSCANNER_AGENT_ID as _DEPSCANNER_AGENT_ID,
    EMAILWRITER_AGENT_ID as _EMAILWRITER_AGENT_ID,
    FINANCIAL_AGENT_ID as _FINANCIAL_AGENT_ID,
    NEGOTIATION_AGENT_ID as _NEGOTIATION_AGENT_ID,
    PORTFOLIO_AGENT_ID as _PORTFOLIO_AGENT_ID,
    PRODUCT_AGENT_ID as _PRODUCT_AGENT_ID,
    QUALITY_JUDGE_AGENT_ID as _QUALITY_JUDGE_AGENT_ID,
    RESUME_AGENT_ID as _RESUME_AGENT_ID,
    SCENARIO_AGENT_ID as _SCENARIO_AGENT_ID,
    SECRETS_AGENT_ID as _SECRETS_AGENT_ID,
    SQLBUILDER_AGENT_ID as _SQLBUILDER_AGENT_ID,
    STATICANALYSIS_AGENT_ID as _STATICANALYSIS_AGENT_ID,
    TEXTINTEL_AGENT_ID as _TEXTINTEL_AGENT_ID,
    WIKI_AGENT_ID as _WIKI_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object
from server.builtin_agents.schemas import quality_judge_input_schema as _quality_judge_input_schema


def load_builtin_specs_part1() -> list[dict[str, Any]]:
    return [
{
    "agent_id": _FINANCIAL_AGENT_ID,
    "name": "Financial Research Agent",
    "description": "Fetches the latest SEC filing and returns a structured investment brief.",
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
    "description": "Staff-engineer-quality code review: OWASP Top 10 vulnerabilities with CWE IDs, performance anti-patterns, complexity scoring, test recommendations, and copy-paste-ready fixes.",
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
    "agent_id": _TEXTINTEL_AGENT_ID,
    "name": "Text Intelligence Agent",
    "description": "Deep NLP analysis: sentiment + objectivity scoring, named entity extraction with roles, logical fallacy detection, rhetorical device identification, bias indicators, and claim extraction. Modes: full | quick | claims | rhetoric.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_TEXTINTEL_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["nlp", "sentiment-analysis", "text-analytics"],
    "input_schema": TextIntelRequest.model_json_schema(),
    "output_schema": _output_schema_object(
        {
            "word_count": {"type": "integer"},
            "reading_time_seconds": {"type": "integer"},
            "language": {"type": "string"},
            "sentiment": {"type": "string"},
            "sentiment_score": {"type": "number"},
            "summary": {"type": "string"},
            "key_entities": {"type": "array", "items": {"type": "string"}},
            "main_topics": {"type": "array", "items": {"type": "string"}},
            "key_quotes": {"type": "array", "items": {"type": "string"}},
        },
        required=["word_count", "summary"],
    ),
    "output_examples": [
        {
            "input": {
                "text": "Revenue rose 18% year over year while operating margin fell 2 points.",
                "mode": "quick",
            },
            "output": {
                "word_count": 13,
                "reading_time_seconds": 4,
                "language": "en",
                "sentiment": "mixed",
                "sentiment_score": 0.12,
                "summary": "Strong growth paired with margin pressure.",
                "key_entities": ["Revenue", "Operating margin"],
                "main_topics": ["earnings", "profitability"],
                "key_quotes": ["Revenue rose 18% year over year"],
            },
        },
        {
            "input": {
                "text": "Customer satisfaction improved after response times dropped below two hours.",
                "mode": "full",
            },
            "output": {
                "word_count": 11,
                "reading_time_seconds": 3,
                "language": "en",
                "sentiment": "positive",
                "sentiment_score": 0.71,
                "summary": "Faster support correlated with better satisfaction.",
                "key_entities": ["Customer satisfaction", "response times"],
                "main_topics": ["support operations", "customer experience"],
                "key_quotes": ["response times dropped below two hours"],
            },
        },
    ],
},
{
    "agent_id": _WIKI_AGENT_ID,
    "name": "Wikipedia Research Agent",
    "description": "Deep research synthesis from Wikipedia: dense fact extraction, chronological timelines, notable figures, statistics with source notes, controversies and debates, knowledge gaps, and primary sources worth following up. Modes: standard | deep.",
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
    "agent_id": _NEGOTIATION_AGENT_ID,
    "name": "Negotiation Strategist Agent",
    "description": "Harvard-method negotiation strategy: ZOPA/BATNA analysis, power dynamics scoring, verbatim scripts, concession sequencing plan, tactic counterplay, and timeline leverage. Grounded in Fisher & Ury and behavioral economics.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_NEGOTIATION_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["negotiation", "strategy", "operations"],
    "input_schema": NegotiationRequest.model_json_schema(),
    "output_schema": _output_schema_object(
        {
            "opening_position": {"type": "string"},
            "must_haves": {"type": "array", "items": {"type": "string"}},
            "tradeables": {"type": "array", "items": {"type": "string"}},
            "red_lines": {"type": "array", "items": {"type": "string"}},
            "tactics": {"type": "array", "items": {"type": "object"}},
            "fallback_plan": {"type": "string"},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
        },
        required=["opening_position", "fallback_plan"],
    ),
    "output_examples": [
        {
            "input": {
                "objective": "Renew enterprise contract at +12% ARR with annual prepay.",
                "counterparty_profile": "Procurement-led team",
                "constraints": ["No discount above 8%"],
                "context": "Incumbent vendor with strong adoption.",
            },
            "output": {
                "opening_position": "Propose multi-year renewal with premium support add-on.",
                "must_haves": ["Price uplift near target", "Annual prepay"],
                "tradeables": ["Seat ramp schedule", "Training credits"],
                "red_lines": ["Discount above 8%"],
                "tactics": [{"name": "anchoring", "description": "Lead with value-backed anchor"}],
                "fallback_plan": "Offer term extension in exchange for lower uplift.",
                "risk_flags": ["Budget freeze risk", "Competitive quotes late in cycle"],
            },
        },
        {
            "input": {
                "objective": "Secure vendor SLA concessions without price increase.",
                "counterparty_profile": "Relationship-focused account team",
                "constraints": ["No budget increase"],
                "context": "Recent outage impacted trust.",
            },
            "output": {
                "opening_position": "Tie SLA upgrades to renewal certainty and reference commitment.",
                "must_haves": ["Response-time SLA improvements"],
                "tradeables": ["Public case study participation"],
                "red_lines": ["Any net new cost"],
                "tactics": [{"name": "package swap", "description": "Exchange non-cash concessions"}],
                "fallback_plan": "Escalate to pilot extension with explicit SLA checkpoints.",
                "risk_flags": ["Vendor legal delays", "Scope ambiguity in SLA wording"],
            },
        },
    ],
},
{
    "agent_id": _SCENARIO_AGENT_ID,
    "name": "Scenario Simulator Agent",
    "description": "5-scenario strategic foresight (crash/downside/base/upside/moonshot) with calibrated probabilities, sensitivity analysis, pre-mortem, monitoring dashboard, and early signal detection. GBN/Shell methodology.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SCENARIO_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["forecasting", "strategy", "decision-making"],
    "input_schema": ScenarioRequest.model_json_schema(),
    "output_schema": _output_schema_object(
        {
            "decision": {"type": "string"},
            "horizon": {"type": "string"},
            "risk_tolerance": {"type": "string"},
            "scenarios": {"type": "array", "items": {"type": "object"}},
            "recommended_plan": {"type": "object"},
            "confidence": {"type": "number"},
        },
        required=["decision", "scenarios", "recommended_plan"],
    ),
    "output_examples": [
        {
            "input": {
                "decision": "Expand to EU via direct sales team",
                "assumptions": "ARR 5M with 30% growth",
                "horizon": "18 months",
                "risk_tolerance": "balanced",
            },
            "output": {
                "decision": "Expand to EU via direct sales team",
                "horizon": "18 months",
                "risk_tolerance": "balanced",
                "scenarios": [
                    {"name": "base", "probability": 0.5, "result": "moderate growth"},
                    {"name": "upside", "probability": 0.25, "result": "accelerated pipeline"},
                ],
                "recommended_plan": {
                    "phases": ["pilot in 2 countries", "scale after KPI validation"]
                },
                "confidence": 0.67,
            },
        },
        {
            "input": {
                "decision": "Delay expansion and deepen US upsell",
                "assumptions": "Strong NRR but slowing top-of-funnel",
                "horizon": "12 months",
                "risk_tolerance": "conservative",
            },
            "output": {
                "decision": "Delay expansion and deepen US upsell",
                "horizon": "12 months",
                "risk_tolerance": "conservative",
                "scenarios": [{"name": "base", "probability": 0.6, "result": "higher cash efficiency"}],
                "recommended_plan": {"focus": ["enterprise expansion", "churn prevention"]},
                "confidence": 0.72,
            },
        },
    ],
},
{
    "agent_id": _PRODUCT_AGENT_ID,
    "name": "Product Strategy Lab Agent",
    "description": "VP-level product strategy: Jobs To Be Done analysis, RICE-scored roadmap, competitive moat assessment, unit economics (CAC/LTV), hypothesis-driven experiments, and phased go-to-market. Honest about weak spots.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_PRODUCT_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["product", "go-to-market", "experimentation"],
    "input_schema": ProductStrategyRequest.model_json_schema(),
    "output_schema": _output_schema_object(
        {
            "positioning_statement": {"type": "string"},
            "user_personas": {"type": "array", "items": {"type": "string"}},
            "roadmap": {"type": "array", "items": {"type": "object"}},
            "experiments": {"type": "array", "items": {"type": "object"}},
            "risks": {"type": "array", "items": {"type": "string"}},
        },
        required=["positioning_statement", "roadmap"],
    ),
    "output_examples": [
        {
            "input": {
                "product_idea": "AI copilot for customer success teams",
                "target_users": "Mid-market B2B SaaS CSMs",
                "market_context": "Crowded tooling category",
                "horizon_quarters": 3,
            },
            "output": {
                "positioning_statement": "Proactive churn prevention assistant for high-volume CSM workflows.",
                "user_personas": ["Scaled CSM", "CS leader"],
                "roadmap": [
                    {"quarter": "Q1", "milestone": "risk scoring MVP"},
                    {"quarter": "Q2", "milestone": "playbook automation"},
                ],
                "experiments": [{"name": "churn model A/B", "metric": "retention lift"}],
                "risks": ["Data quality variance", "Integration complexity"],
            },
        },
        {
            "input": {
                "product_idea": "Automated onboarding coach for PLG products",
                "target_users": "SMB product teams",
                "market_context": "High trial-to-paid drop-off",
                "horizon_quarters": 2,
            },
            "output": {
                "positioning_statement": "Guided activation coach that shortens time-to-value for new users.",
                "user_personas": ["Growth PM", "Lifecycle marketer"],
                "roadmap": [{"quarter": "Q1", "milestone": "in-app assistant + milestone tracking"}],
                "experiments": [{"name": "activation checklist personalization", "metric": "activation rate"}],
                "risks": ["Over-personalization fatigue"],
            },
        },
    ],
},
{
    "agent_id": _PORTFOLIO_AGENT_ID,
    "name": "Portfolio Planner Agent",
    "description": "CFA-level portfolio planning: mean-variance optimization concepts, factor exposure, Sharpe/Sortino estimates, inflation-adjusted return ranges, tax efficiency notes, specific ETF examples (VTI/BND/VXUS), phased deployment plan, and realistic red flags.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_PORTFOLIO_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["portfolio", "allocation", "wealth-planning"],
    "input_schema": PortfolioRequest.model_json_schema(),
    "output_schema": _output_schema_object(
        {
            "goal_summary": {"type": "string"},
            "allocation": {"type": "array", "items": {"type": "object"}},
            "rebalancing_plan": {"type": "string"},
            "watch_metrics": {"type": "array", "items": {"type": "string"}},
            "disclaimer": {"type": "string"},
        },
        required=["goal_summary", "allocation"],
    ),
    "output_examples": [
        {
            "input": {
                "investment_goal": "Long-term wealth growth",
                "risk_profile": "balanced",
                "time_horizon_years": 10,
                "capital_usd": 50000,
            },
            "output": {
                "goal_summary": "Balanced growth allocation for long-term horizon.",
                "allocation": [
                    {"asset_class": "US equities", "weight_pct": 45},
                    {"asset_class": "International equities", "weight_pct": 20},
                    {"asset_class": "Bonds", "weight_pct": 30},
                    {"asset_class": "Cash", "weight_pct": 5},
                ],
                "rebalancing_plan": "Rebalance semi-annually or at 5% drift.",
                "watch_metrics": ["volatility", "drawdown", "allocation drift"],
                "disclaimer": "Educational output, not investment advice.",
            },
        },
        {
            "input": {
                "investment_goal": "Capital preservation",
                "risk_profile": "conservative",
                "time_horizon_years": 5,
                "capital_usd": 120000,
            },
            "output": {
                "goal_summary": "Conservative allocation prioritizing downside protection.",
                "allocation": [
                    {"asset_class": "Investment-grade bonds", "weight_pct": 55},
                    {"asset_class": "Dividend equities", "weight_pct": 25},
                    {"asset_class": "Cash equivalents", "weight_pct": 20},
                ],
                "rebalancing_plan": "Quarterly review with annual tax-aware rebalance.",
                "watch_metrics": ["income yield", "duration risk", "inflation sensitivity"],
                "disclaimer": "Educational output, not investment advice.",
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
    "agent_id": _RESUME_AGENT_ID,
    "name": "Resume Analyzer Agent",
    "description": "Staff-recruiter-quality resume analysis: ATS score, keyword gap detection, line-by-line rewrites, section audit, and a verdict. Optionally matches against a specific job description.",
    "endpoint_url": "internal://resume",
    "price_per_call_usd": 0.02,
    "tags": ["career", "recruiting", "writing"],
    "input_schema": {
        "type": "object",
        "properties": {
            "resume_text": {
                "type": "string",
                "description": "Full resume text (plain text or lightly formatted)",
                "example": "Jane Doe\njane@email.com\n\nExperience:\nSoftware Engineer at Acme Corp...",
            },
            "job_description": {
                "type": "string",
                "description": "Job description to match against (optional)",
                "default": "",
            },
            "role_level": {
                "type": "string",
                "enum": ["junior", "mid", "senior", "executive"],
                "default": "mid",
                "description": "Target seniority level",
            },
        },
        "required": ["resume_text"],
    },
    "output_schema": _output_schema_object(
        {
            "overall_score": {"type": "integer"},
            "ats_score": {"type": "integer"},
            "verdict": {"type": "string"},
            "strengths": {"type": "array", "items": {"type": "string"}},
            "critical_gaps": {"type": "array", "items": {"type": "string"}},
            "line_edits": {"type": "array", "items": {"type": "object"}},
            "one_line_summary": {"type": "string"},
        },
        required=["overall_score", "verdict", "one_line_summary"],
    ),
    "output_examples": [
        {
            "input": {"resume_text": "John Smith\njohn@email.com\n\nExperience:\nJr Dev at StartupXYZ 2022-2024...", "role_level": "mid"},
            "output": {
                "overall_score": 62,
                "ats_score": 71,
                "verdict": "needs_work",
                "strengths": ["Consistent employment history", "Relevant tech stack listed"],
                "critical_gaps": ["No quantified impact in any bullet", "Missing summary section", "Skills section is disorganized"],
                "line_edits": [{"original": "Worked on features", "improved": "Shipped 8 product features serving 12K users, reducing support tickets 22%", "reason": "Quantified impact outperforms vague ownership"}],
                "one_line_summary": "Solid background but resume undersells their work — needs rewriting before senior roles.",
            },
        },
    ],
},
{
    "agent_id": _SQLBUILDER_AGENT_ID,
    "name": "SQL Query Builder Agent",
    "description": "Natural language to production SQL across PostgreSQL, MySQL, SQLite, BigQuery, and Snowflake. Includes explanation, edge case handling, performance notes, and dialect-specific guidance.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SQLBUILDER_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["sql", "data-engineering", "developer-tools"],
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Natural language question to answer with SQL",
                "example": "What are the top 10 customers by total spend in the last 90 days?",
            },
            "schema": {
                "type": "string",
                "description": "Database schema as DDL or table descriptions (optional)",
                "default": "",
                "example": "CREATE TABLE orders (id INT, customer_id INT, amount DECIMAL, created_at TIMESTAMP);",
            },
            "dialect": {
                "type": "string",
                "enum": ["postgresql", "mysql", "sqlite", "bigquery", "snowflake"],
                "default": "postgresql",
            },
            "context": {
                "type": "string",
                "description": "Additional context: data volumes, performance requirements",
                "default": "",
            },
        },
        "required": ["question"],
    },
    "output_schema": _output_schema_object(
        {
            "sql": {"type": "string"},
            "explanation": {"type": "string"},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "performance_notes": {"type": "array", "items": {"type": "string"}},
            "estimated_complexity": {"type": "string"},
        },
        required=["sql", "explanation"],
    ),
    "output_examples": [
        {
            "input": {
                "question": "Top 5 products by revenue last quarter",
                "schema": "CREATE TABLE orders (id INT, product_id INT, amount DECIMAL, created_at TIMESTAMP);\nCREATE TABLE products (id INT, name TEXT);",
                "dialect": "postgresql",
            },
            "output": {
                "sql": "WITH last_q AS (\n  SELECT product_id, SUM(amount) AS revenue\n  FROM orders\n  WHERE created_at >= date_trunc('quarter', CURRENT_DATE) - INTERVAL '3 months'\n    AND created_at < date_trunc('quarter', CURRENT_DATE)\n  GROUP BY product_id\n)\nSELECT p.name, lq.revenue\nFROM last_q lq JOIN products p ON p.id = lq.product_id\nORDER BY lq.revenue DESC\nLIMIT 5;",
                "explanation": "Uses date_trunc to isolate the previous calendar quarter, aggregates order revenue per product, joins to get names, and returns top 5.",
                "assumptions": ["'Last quarter' means previous full calendar quarter", "amount column is already in the same currency"],
                "performance_notes": ["Add index on orders(created_at, product_id) for large tables"],
                "estimated_complexity": "moderate",
            },
        },
    ],
},
{
    "agent_id": _DATAINSIGHTS_AGENT_ID,
    "name": "Data Insights Agent",
    "description": "Analyzes JSON, CSV, or structured text data: descriptive statistics, anomaly detection, trend identification, direct answers to specific questions, and visualization recommendations.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_DATAINSIGHTS_AGENT_ID],
    "price_per_call_usd": 0.02,
    "tags": ["data-analysis", "analytics", "statistics"],
    "input_schema": {
        "type": "object",
        "properties": {
            "data": {
                "type": "string",
                "description": "Raw data to analyze: JSON array, CSV text, or key:value pairs",
                "example": '[{"month":"Jan","revenue":42000,"users":1200},{"month":"Feb","revenue":51000,"users":1450}]',
            },
            "question": {
                "type": "string",
                "description": "Specific question to answer, or 'general' for open-ended analysis",
                "default": "general",
                "example": "Which month had the highest revenue per user?",
            },
            "format": {
                "type": "string",
                "enum": ["json", "csv", "text"],
                "default": "json",
            },
        },
        "required": ["data"],
    },
    "output_schema": _output_schema_object(
        {
            "row_count": {"type": "integer"},
            "key_findings": {"type": "array", "items": {"type": "string"}},
            "anomalies": {"type": "array", "items": {"type": "object"}},
            "answer_to_question": {"type": "string"},
            "recommendations": {"type": "array", "items": {"type": "string"}},
        },
        required=["key_findings", "answer_to_question"],
    ),
    "output_examples": [
        {
            "input": {
                "data": '[{"month":"Jan","revenue":42000},{"month":"Feb","revenue":51000},{"month":"Mar","revenue":38000}]',
                "question": "Is revenue trending up or down?",
                "format": "json",
            },
            "output": {
                "row_count": 3,
                "key_findings": ["Feb was peak revenue at $51K", "March dropped 25.5% from Feb — significant decline", "Jan-Feb shows growth but Feb-Mar reversal"],
                "anomalies": [{"description": "March revenue drop of 25.5% from Feb — unusually large swing", "severity": "medium", "affected_rows": "row 3"}],
                "answer_to_question": "Mixed — revenue grew 21% Jan to Feb, then dropped 25.5% in March. No clear trend in 3 data points; more history needed.",
                "recommendations": ["Investigate March drop cause before drawing conclusions", "Collect at least 6 months of data for trend analysis"],
            },
        },
    ],
},
{
    "agent_id": _EMAILWRITER_AGENT_ID,
    "name": "Email Sequence Writer Agent",
    "description": "Writes professional emails and multi-email sequences for outreach, follow-ups, proposals, announcements, and support. Generates 3 subject line A/B variants, preview text, and personalization hooks per email.",
    "endpoint_url": "internal://email-writer",
    "price_per_call_usd": 0.02,
    "tags": ["writing", "marketing", "sales"],
    "input_schema": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "What this email (or sequence) needs to achieve",
                "example": "Book a 20-minute demo with a VP of Engineering at a Series B startup",
            },
            "tone": {
                "type": "string",
                "enum": ["formal", "professional", "friendly", "direct", "persuasive"],
                "default": "professional",
            },
            "email_type": {
                "type": "string",
                "enum": ["outreach", "follow_up", "proposal", "rejection", "announcement", "support", "sequence"],
                "default": "outreach",
            },
            "recipient_context": {
                "type": "string",
                "description": "Who you're writing to",
                "default": "",
                "example": "VP Engineering at a 50-person Series B SaaS startup in fintech",
            },
            "sender_context": {
                "type": "string",
                "description": "Who you are / your company",
                "default": "",
            },
            "key_points": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key points to include",
                "default": [],
            },
            "sequence_length": {
                "type": "integer",
                "description": "Number of emails in the sequence (1-5)",
                "default": 1,
                "minimum": 1,
                "maximum": 5,
            },
        },
        "required": ["goal"],
    },
    "output_schema": _output_schema_object(
        {
            "emails": {"type": "array", "items": {"type": "object"}},
            "strategy_notes": {"type": "string"},
            "personalization_hooks": {"type": "array", "items": {"type": "string"}},
        },
        required=["emails", "strategy_notes"],
    ),
    "output_examples": [
        {
            "input": {
                "goal": "Get a product manager to respond to a demo request",
                "tone": "friendly",
                "email_type": "outreach",
                "recipient_context": "Senior PM at a mid-size B2B SaaS company",
                "sequence_length": 1,
            },
            "output": {
                "emails": [{
                    "sequence_position": 1,
                    "subject_lines": ["Quick question about [Company]'s onboarding flow", "The problem most PMs have with {metric}", "15 minutes — is this relevant to you?"],
                    "body": "Hi [Name],\n\nI noticed [Company] recently launched [feature] — that usually means onboarding optimization becomes a real priority.\n\nWe help teams like yours cut time-to-value by 40% without touching the engineering backlog.\n\nWorth 15 minutes next week?\n\n[Your name]",
                    "preview_text": "Quick question about your onboarding flow at [Company]...",
                    "send_timing": "Day 0",
                    "word_count": 58,
                    "cta": "Book a 15-minute call",
                }],
                "strategy_notes": "Single email focused on a specific trigger event to establish relevance before the ask.",
                "personalization_hooks": ["Reference a recent product launch or announcement", "Mention a job posting that reveals a pain point", "Cite a public metric or company news"],
            },
        },
    ],
},
{
    "agent_id": _SECRETS_AGENT_ID,
    "name": "Secrets Detection Agent",
    "description": "Scans GitHub repositories for exposed API keys, credentials, tokens, and secrets in source code and git history. Detects Stripe keys, AWS credentials, JWT secrets, and database passwords.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SECRETS_AGENT_ID],
    "price_per_call_usd": 0.04,
    "tags": ["security", "secrets", "git-history", "credentials"],
    "input_schema": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "GitHub repo (owner/repo or full URL)"},
            "scan": {"type": "string", "enum": ["full", "shallow"], "default": "full", "description": "full scans git history; shallow scans current HEAD only"},
            "branch": {"type": "string", "default": "main"},
        },
        "required": ["repo"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "secrets": {"type": "array", "items": {"type": "object"}},
            "git_history_secrets": {"type": "array", "items": {"type": "object"}},
            "total_critical": {"type": "integer"},
            "summary": {"type": "string"},
        },
    },
    "output_examples": [
        {
            "input": {"repo": "acme/payments-api", "scan": "full"},
            "output": {
                "repo": "acme/payments-api",
                "secrets": [{"file": "src/config/keys.js", "line": 12, "type": "stripe_key", "description": "Hardcoded Stripe live secret key", "confidence": "high"}],
                "git_history_secrets": [{"commit": "a3f9b12", "file": ".env.backup", "type": "aws_credentials", "description": "AWS credentials committed to git history"}],
                "total_critical": 2,
                "summary": "Found 2 critical credential exposures.",
                "scan_duration_ms": 1100,
            },
        },
    ],
},
{
    "agent_id": _STATICANALYSIS_AGENT_ID,
    "name": "Static Analysis Agent",
    "description": "Performs static security analysis on GitHub repositories. Detects SQL injection (CWE-89), authentication bypass (CWE-306), XSS (CWE-79), path traversal (CWE-22), and SSRF (CWE-918) with copy-paste-ready fixes.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_STATICANALYSIS_AGENT_ID],
    "price_per_call_usd": 0.09,
    "tags": ["security", "sast", "code-analysis", "cwe", "owasp"],
    "input_schema": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "GitHub repo (owner/repo or full URL)"},
            "focus": {"type": "string", "default": "all", "description": "Comma-separated: injection,auth,xss,path_traversal,all"},
            "language": {"type": "string", "default": "auto"},
        },
        "required": ["repo"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "findings": {"type": "array", "items": {"type": "object"}},
            "total_critical": {"type": "integer"},
            "total_high": {"type": "integer"},
            "summary": {"type": "string"},
        },
    },
    "output_examples": [
        {
            "input": {"repo": "acme/payments-api", "focus": "injection,auth"},
            "output": {
                "repo": "acme/payments-api",
                "findings": [{"file": "src/db/query.js", "line": 47, "severity": "critical", "type": "sql_injection", "cwe": "CWE-89", "description": "Unsanitized input in SQL query"}],
                "total_critical": 1,
                "total_high": 1,
                "summary": "Found 1 critical SQL injection (CWE-89).",
                "scan_duration_ms": 2300,
            },
        },
    ],
},
{
    "agent_id": _DEPSCANNER_AGENT_ID,
    "name": "Dependency Scanner Agent",
    "description": "Scans npm, pip, Maven, Cargo, and Go module dependencies against the NIST NVD and GitHub Advisory Database. Returns CVEs with CVSS scores, affected version ranges, and upgrade paths.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_DEPSCANNER_AGENT_ID],
    "price_per_call_usd": 0.11,
    "tags": ["security", "dependencies", "cve", "supply-chain", "npm"],
    "input_schema": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "GitHub repo to scan"},
            "ecosystem": {"type": "string", "enum": ["npm", "pip", "maven", "cargo", "go"], "default": "npm"},
        },
        "required": ["repo"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "vulnerabilities": {"type": "array", "items": {"type": "object"}},
            "total": {"type": "integer"},
            "summary": {"type": "string"},
        },
    },
    "output_examples": [
        {
            "input": {"repo": "acme/payments-api", "ecosystem": "npm"},
            "output": {
                "repo": "acme/payments-api",
                "ecosystem": "npm",
                "vulnerabilities": [{"package": "lodash", "version": "4.17.20", "cve": "CVE-2021-23337", "cvss": 7.2, "severity": "high"}],
                "total": 2,
                "summary": "Found 2 CVEs across 1 vulnerable package.",
                "scan_duration_ms": 3100,
            },
        },
    ],
},
{
    "agent_id": _CVELOOKUP_AGENT_ID,
    "name": "CVE Lookup Agent",
    "description": "Real-time CVE intelligence for specific package versions. Cross-references NIST NVD, MITRE CVE, and GitHub Advisory Database. Returns CVSS scores, exploit availability, affected version ranges, and recommended upgrade paths.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_CVELOOKUP_AGENT_ID],
    "price_per_call_usd": 0.06,
    "tags": ["security", "cve", "vulnerability-intel", "nvd", "packages"],
    "input_schema": {
        "type": "object",
        "properties": {
            "packages": {"type": "array", "items": {"type": "string"}, "description": "Array of package@version strings", "example": ["express@4.17.1", "lodash@4.17.20"]},
            "include_patched": {"type": "boolean", "default": False},
        },
        "required": ["packages"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "results": {"type": "array", "items": {"type": "object"}},
            "total_vulnerable": {"type": "integer"},
            "summary": {"type": "string"},
        },
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
