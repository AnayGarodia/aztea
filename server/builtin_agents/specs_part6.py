"""Sixth chunk of built-in agent specs (YC-demo agents added 2026-05-09).

This shard registers the six agents that anchor the launch-audit demo:
Lighthouse, axe-core accessibility, security-headers grader, broken-link
crawler, PDF parser, and DuckDuckGo web search. Kept separate from
``specs_part2`` to respect the < 1000-line per-file budget.
"""

from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    ACCESSIBILITY_AUDITOR_AGENT_ID as _ACCESSIBILITY_AUDITOR_AGENT_ID,
)
from server.builtin_agents.constants import (
    BROKEN_LINK_CRAWLER_AGENT_ID as _BROKEN_LINK_CRAWLER_AGENT_ID,
)
from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
)
from server.builtin_agents.constants import (
    LIGHTHOUSE_AUDITOR_AGENT_ID as _LIGHTHOUSE_AUDITOR_AGENT_ID,
)
from server.builtin_agents.constants import (
    PDF_DOCUMENT_PARSER_AGENT_ID as _PDF_DOCUMENT_PARSER_AGENT_ID,
)
from server.builtin_agents.constants import (
    SECURITY_HEADERS_GRADER_AGENT_ID as _SECURITY_HEADERS_GRADER_AGENT_ID,
)
from server.builtin_agents.constants import (
    WEB_SEARCH_AGENT_ID as _WEB_SEARCH_AGENT_ID,
)


def load_builtin_specs_part6() -> list[dict[str, Any]]:
    return [
        # ── Lighthouse Auditor ──────────────────────────────────────────────────
        {
            "agent_id": _LIGHTHOUSE_AUDITOR_AGENT_ID,
            "name": "Lighthouse Auditor",
            "description": "Use when the task requires real Web Vitals + perf / a11y / SEO scoring of a public URL. Runs Google Lighthouse in headless Chromium and returns category scores (0-100), key metrics (LCP, FCP, CLS, TBT, TTI), top opportunities sorted by potential savings, and the failed-audit list. ~30s per call.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_LIGHTHOUSE_AUDITOR_AGENT_ID],
            "price_per_call_usd": 0.05,
            "tags": [
                "performance",
                "lighthouse",
                "web-vitals",
                "seo",
                "frontend",
                "quality",
            ],
            "match_keywords": [
                "lighthouse",
                "web vitals",
                "core web vitals",
                "lcp",
                "cls",
                "fcp",
                "performance audit",
                "page speed",
                "pagespeed",
                "seo score",
                "lighthouse score",
            ],
            "kind": "aztea_built",
            "category": "Quality",
            "is_featured": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Public URL to audit (https recommended).",
                        "format": "uri",
                    },
                    "categories": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "performance",
                                "accessibility",
                                "best-practices",
                                "seo",
                                "pwa",
                            ],
                        },
                        "description": "Lighthouse categories to run. Defaults to all four core categories.",
                    },
                    "strategy": {
                        "type": "string",
                        "enum": ["mobile", "desktop"],
                        "default": "mobile",
                        "description": "Form factor for the run.",
                    },
                    "throttling": {
                        "type": "string",
                        "enum": ["simulate", "provided", "devtools"],
                        "description": (
                            "Lighthouse --throttling-method override. Omit "
                            "to derive from strategy (mobile=simulate, "
                            "desktop=provided). 'devtools' applies real "
                            "browser-level throttling; 'provided' uses the "
                            "connection as-is."
                        ),
                    },
                    "max_wait_seconds": {
                        "type": "integer",
                        "minimum": 20,
                        "maximum": 180,
                        "default": 90,
                        "description": "Hard timeout for the lighthouse subprocess.",
                    },
                },
                "required": ["url"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "scores": {"type": "object"},
                    "metrics": {"type": "object"},
                    "top_opportunities": {"type": "array"},
                    "failed_audits": {"type": "array"},
                    "billing_units_actual": {"type": "integer"},
                },
                "required": ["url", "scores", "metrics", "billing_units_actual"],
            },
            "output_examples": [
                {
                    "input": {"url": "https://example.com", "strategy": "mobile"},
                    "output": {
                        "url": "https://example.com",
                        "final_url": "https://example.com/",
                        "fetch_time": "2026-05-09T12:00:00.000Z",
                        "strategy": "mobile",
                        "lighthouse_version": "11.0.0",
                        "scores": {
                            "performance": 71,
                            "accessibility": 92,
                            "best_practices": 96,
                            "seo": 100,
                            "pwa": None,
                        },
                        "metrics": {
                            "lcp_ms": 3400,
                            "fcp_ms": 1100,
                            "cls": 0.04,
                            "tbt_ms": 280,
                            "tti_ms": 4800,
                            "speed_index_ms": 2900,
                        },
                        "top_opportunities": [
                            {
                                "id": "uses-optimized-images",
                                "title": "Efficiently encode images",
                                "savings_ms": 1200,
                                "description": "Optimized images load faster and consume less cellular data.",
                            }
                        ],
                        "failed_audits": [
                            {
                                "id": "render-blocking-resources",
                                "category": "performance",
                                "title": "Eliminate render-blocking resources",
                                "score": 0.45,
                            }
                        ],
                        "billing_units_actual": 1,
                    },
                }
            ],
        },
        # ── Accessibility Auditor ───────────────────────────────────────────────
        {
            "agent_id": _ACCESSIBILITY_AUDITOR_AGENT_ID,
            "name": "Accessibility Auditor",
            "description": "Use when the task requires checking a web page for WCAG accessibility violations. Loads the URL in a real Chromium browser, injects axe-core 4.x (CDN with a vendored offline fallback — axe_source reports which ran), and returns structured violations grouped by rule with the affected DOM nodes plus the manual-review `incomplete` checks. Defaults to the WCAG-2.1-AA tag set. Best evaluated against content-rich pages (e.g., Wikipedia or a real product site) — single-paragraph stubs like example.com don't exercise the rule engine.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_ACCESSIBILITY_AUDITOR_AGENT_ID],
            "price_per_call_usd": 0.03,
            "tags": [
                "accessibility",
                "a11y",
                "wcag",
                "axe-core",
                "frontend",
                "quality",
            ],
            "match_keywords": [
                "accessibility",
                "a11y",
                "wcag",
                "axe-core",
                "axe core",
                "screen reader",
                "aria",
                "alt text",
                "contrast ratio",
                "wcag 2.1",
                "wcag 2.2",
            ],
            "kind": "aztea_built",
            "category": "Quality",
            "is_featured": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Public URL to audit.",
                        "format": "uri",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "axe-core tag filter, e.g. ['wcag2a','wcag2aa','wcag21aa']. Defaults to the AA bundle.",
                    },
                    "viewport": {
                        "type": "object",
                        "properties": {
                            "width": {"type": "integer", "minimum": 320, "maximum": 3840},
                            "height": {"type": "integer", "minimum": 240, "maximum": 2160},
                        },
                    },
                    "wait_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 8000,
                        "default": 1500,
                        "description": "Extra wait after networkidle before running axe.",
                    },
                },
                "required": ["url"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "violations": {"type": "array"},
                    "violations_truncated": {
                        "type": "boolean",
                        "description": "True when more than 30 violations were found (totals.violations has the real count)",
                    },
                    "incomplete": {
                        "type": "array",
                        "description": "Checks axe could not auto-decide (manual review needed), as {id, impact, help, help_url, node_count} — often the most important findings",
                    },
                    "axe_source": {
                        "type": "string",
                        "enum": ["cdn", "vendored"],
                        "description": "Where axe-core was loaded from this run",
                    },
                    "unknown_tags": {
                        "type": "array",
                        "description": "Advisory: requested tags that don't match axe's tag grammar (likely typos auditing zero rules)",
                    },
                    "totals": {"type": "object"},
                    "billing_units_actual": {"type": "integer"},
                },
                "required": ["url", "violations", "totals", "billing_units_actual"],
            },
            "output_examples": [
                {
                    "input": {"url": "https://example.com"},
                    "output": {
                        "url": "https://example.com",
                        "final_url": "https://example.com/",
                        "page_title": "Example Domain",
                        "axe_version": "4.8.4",
                        "test_engine": "axe-core",
                        "violations": [
                            {
                                "id": "color-contrast",
                                "impact": "serious",
                                "tags": ["wcag2aa", "wcag143"],
                                "help": "Elements must have sufficient color contrast",
                                "help_url": "https://dequeuniversity.com/rules/axe/4.8/color-contrast",
                                "node_count": 3,
                                "nodes": [
                                    {
                                        "target": [".cta-secondary"],
                                        "html": "<a class=\"cta-secondary\" href=\"/docs\">Read the docs</a>",
                                        "failure_summary": "Fix any of the following: Element has insufficient color contrast of 3.2:1.",
                                    }
                                ],
                            }
                        ],
                        "totals": {
                            "violations": 1,
                            "critical": 0,
                            "serious": 1,
                            "moderate": 0,
                            "minor": 0,
                            "passes": 38,
                            "incomplete": 2,
                        },
                        "execution_time_ms": 4200,
                        "billing_units_actual": 1,
                    },
                }
            ],
        },
        # ── Security Headers Grader ─────────────────────────────────────────────
        {
            "agent_id": _SECURITY_HEADERS_GRADER_AGENT_ID,
            "name": "Security Headers Grader",
            "description": "Use when the task requires grading the HTTP security headers of a public URL: CSP, HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, COOP/COEP/CORP. Returns a 0-100 score, A+/A/B/.../F letter grade, list of missing/weak headers, and the redirect chain.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SECURITY_HEADERS_GRADER_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["security", "http-headers", "csp", "hsts", "web-security"],
            "match_keywords": [
                "security headers",
                "csp",
                "content security policy",
                "hsts",
                "x-frame-options",
                "permissions-policy",
                "referrer-policy",
                "cors",
                "coop",
                "coep",
                "securityheaders.com",
            ],
            "kind": "aztea_built",
            "category": "Security",
            "is_featured": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch and grade.",
                        "format": "uri",
                    },
                    "follow_redirects": {
                        "type": "boolean",
                        "default": True,
                        "description": "Follow HTTP redirects (max 5 hops).",
                    },
                },
                "required": ["url"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "grade": {"type": "string"},
                    "score": {"type": "integer"},
                    "headers": {"type": "object"},
                    "missing": {"type": "array", "items": {"type": "string"}},
                    "weak": {"type": "array"},
                    "billing_units_actual": {"type": "integer"},
                },
                "required": ["grade", "score", "headers", "billing_units_actual"],
            },
            "output_examples": [
                {
                    "input": {"url": "https://example.com"},
                    "output": {
                        "url": "https://example.com",
                        "final_url": "https://example.com/",
                        "status_code": 200,
                        "redirect_chain": [
                            {"url": "https://example.com", "status_code": 200}
                        ],
                        "grade": "B",
                        "score": 72,
                        "headers": {
                            "strict_transport_security": "max-age=31536000",
                            "content_security_policy": None,
                            "x_frame_options": "SAMEORIGIN",
                            "x_content_type_options": "nosniff",
                            "referrer_policy": "strict-origin-when-cross-origin",
                            "permissions_policy": None,
                            "cross_origin_opener_policy": None,
                            "cross_origin_embedder_policy": None,
                            "cross_origin_resource_policy": None,
                        },
                        "missing": ["content-security-policy", "permissions-policy"],
                        "weak": [
                            {
                                "header": "strict-transport-security",
                                "issue": "missing 'includeSubDomains'",
                            }
                        ],
                        "passed": ["x-content-type-options", "x-frame-options", "referrer-policy"],
                        "leaky_headers": ["server"],
                        "tls": {"is_https": True},
                        "billing_units_actual": 1,
                    },
                }
            ],
        },
        # ── Broken Link Crawler ─────────────────────────────────────────────────
        {
            "agent_id": _BROKEN_LINK_CRAWLER_AGENT_ID,
            "name": "Broken Link Crawler",
            "description": "Use when the task requires crawling a website to find broken links, redirect chains, mixed-content warnings, or images missing alt text. Same-origin BFS crawl with bounded concurrency. Reports HTTP 4xx/5xx, network failures, and HTTPS pages loading HTTP assets.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_BROKEN_LINK_CRAWLER_AGENT_ID],
            "price_per_call_usd": 0.04,
            "tags": ["crawler", "broken-links", "frontend", "quality", "site-audit"],
            "match_keywords": [
                "broken links",
                "broken link",
                "404 check",
                "site crawl",
                "crawl",
                "mixed content",
                "missing alt",
                "alt text",
                "redirect chain",
                "site audit",
            ],
            "kind": "aztea_built",
            "category": "Quality",
            "is_featured": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Seed URL — crawler walks the same origin from here.",
                        "format": "uri",
                    },
                    "max_pages": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 25,
                    },
                    "max_depth": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4,
                        "default": 2,
                    },
                    "include_external": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, HEAD-checks first-hop external links.",
                    },
                    "check_images": {
                        "type": "boolean",
                        "default": True,
                        "description": "Audit <img> elements for missing alt attributes.",
                    },
                },
                "required": ["url"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "pages_crawled": {"type": "integer"},
                    "links_checked": {"type": "integer"},
                    "broken_links": {"type": "array"},
                    "redirect_chains": {"type": "array"},
                    "mixed_content": {"type": "array"},
                    "missing_alt_text": {"type": "array"},
                    "summary": {"type": "object"},
                    "billing_units_actual": {"type": "integer"},
                },
                "required": ["pages_crawled", "broken_links", "summary", "billing_units_actual"],
            },
            "variable_pricing": {
                "model": "tiered",
                "field": "max_pages",
                "field_type": "integer",
                "unit_label": "page",
                "tiers": [
                    {"max_units": 5, "price_usd": 0.02},
                    {"max_units": 15, "price_usd": 0.04},
                    {"max_units": 30, "price_usd": 0.07},
                    {"max_units": 50, "price_usd": 0.10},
                ],
            },
            "output_examples": [
                {
                    "input": {"url": "https://example.com", "max_pages": 10, "max_depth": 1},
                    "output": {
                        "seed_url": "https://example.com",
                        "origin": "https://example.com",
                        "pages_crawled": 8,
                        "links_checked": 47,
                        "broken_links": [
                            {
                                "url": "https://example.com/old-blog-post",
                                "status_code": 404,
                                "found_on": "https://example.com/blog",
                                "reason": "HTTP 404",
                            }
                        ],
                        "redirect_chains": [],
                        "mixed_content": [],
                        "missing_alt_text": [
                            {
                                "page_url": "https://example.com/about",
                                "img_src": "https://example.com/team.jpg",
                            }
                        ],
                        "summary": {
                            "broken_count": 1,
                            "redirects_count": 0,
                            "mixed_content_count": 0,
                            "missing_alt_count": 1,
                        },
                        "billing_units_actual": 8,
                    },
                }
            ],
        },
        # ── PDF Document Parser ─────────────────────────────────────────────────
        {
            "agent_id": _PDF_DOCUMENT_PARSER_AGENT_ID,
            "name": "PDF Document Parser",
            "description": "Use when the task requires fetching a PDF URL and extracting structured text, tables, and metadata. Returns per-page text, document metadata (title, author, dates), and best-effort tabular extraction. Hard-capped at 100 pages and 25 MB.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_PDF_DOCUMENT_PARSER_AGENT_ID],
            "price_per_call_usd": 0.02,
            "tags": ["pdf", "document", "extraction", "research", "tables"],
            "match_keywords": [
                "pdf",
                "extract pdf",
                "parse pdf",
                "pdf to text",
                "pdf tables",
                "research paper",
                "whitepaper",
                "document parsing",
                "pdf metadata",
            ],
            "kind": "aztea_built",
            "category": "Research",
            "is_featured": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL of a public PDF.",
                        "format": "uri",
                    },
                    "max_pages": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 50,
                    },
                    "include_tables": {
                        "type": "boolean",
                        "default": True,
                        "description": "Best-effort tabular extraction via pdfplumber.",
                    },
                    "max_text_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 200000,
                        "default": 60000,
                        "description": "Truncation guard for the joined text field.",
                    },
                },
                "required": ["url"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "page_count": {"type": "integer"},
                    "pages_returned": {"type": "integer"},
                    "metadata": {
                        "type": "object",
                        "description": (
                            "Standard PDF metadata. ``title_source`` is one "
                            "of ``embedded`` (read from the PDF metadata "
                            "dict), ``page1_heuristic`` (extracted from the "
                            "largest-font line in the top 30%% of page 1 "
                            "when embedded title was empty), or ``null`` "
                            "(no plausible title found)."
                        ),
                        "properties": {
                            "title": {"type": ["string", "null"]},
                            "title_source": {
                                "type": ["string", "null"],
                                "enum": ["embedded", "page1_heuristic", None],
                            },
                            "author": {"type": ["string", "null"]},
                            "subject": {"type": ["string", "null"]},
                            "creator": {"type": ["string", "null"]},
                            "producer": {"type": ["string", "null"]},
                            "creation_date": {"type": ["string", "null"]},
                        },
                    },
                    "text": {"type": "string"},
                    "pages": {"type": "array"},
                    "tables": {"type": "array"},
                    "billing_units_actual": {"type": "integer"},
                },
                "required": ["page_count", "text", "billing_units_actual"],
            },
            "variable_pricing": {
                "model": "tiered",
                "field": "max_pages",
                "field_type": "integer",
                "unit_label": "page",
                "tiers": [
                    {"max_units": 5, "price_usd": 0.01},
                    {"max_units": 25, "price_usd": 0.02},
                    {"max_units": 60, "price_usd": 0.04},
                    {"max_units": 100, "price_usd": 0.08},
                ],
            },
            "output_examples": [
                {
                    "input": {"url": "https://arxiv.org/pdf/1706.03762"},
                    "output": {
                        "url": "https://arxiv.org/pdf/1706.03762",
                        "page_count": 15,
                        "pages_returned": 15,
                        "metadata": {
                            "title": "Attention Is All You Need",
                            "title_source": "embedded",
                            "author": "Vaswani et al.",
                            "subject": None,
                            "creator": "LaTeX",
                            "producer": "pdfTeX",
                            "creation_date": "D:20170612000000Z",
                        },
                        "text": "Attention Is All You Need\n\nAshish Vaswani, Noam Shazeer ...",
                        "pages": [
                            {
                                "page": 1,
                                "text": "Attention Is All You Need\n\n...",
                                "char_count": 4200,
                            }
                        ],
                        "tables": [
                            {
                                "page": 8,
                                "rows": 6,
                                "cols": 5,
                                "preview": [
                                    ["Model", "BLEU EN-DE", "BLEU EN-FR", "Params", "FLOPs"]
                                ],
                            }
                        ],
                        "billing_units_actual": 15,
                    },
                }
            ],
        },
        # ── Web Search (DuckDuckGo) ─────────────────────────────────────────────
        {
            "agent_id": _WEB_SEARCH_AGENT_ID,
            "name": "Web Search",
            "description": "Use when the task requires searching the live web. Calls DuckDuckGo's HTML endpoint (no API key required) and returns ranked results with title, URL, description, and site name. Supports country region filter and freshness window (past day/week/month/year).",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_WEB_SEARCH_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["search", "web", "news", "research", "live-data"],
            "match_keywords": [
                "web search",
                "google search",
                "search the web",
                "serp",
                "duckduckgo search",
                "live web",
                "current events",
                "news search",
                "latest news",
            ],
            "kind": "aztea_built",
            "category": "Research",
            "is_featured": True,
            "runtime_requirements": [],
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, 1-400 chars.",
                        "minLength": 1,
                        "maxLength": 400,
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 10,
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["web", "news"],
                        "default": "web",
                    },
                    "country": {
                        "type": "string",
                        "description": "ISO-3166-1 alpha-2 country code (e.g. US, GB).",
                    },
                    "freshness": {
                        "type": "string",
                        "enum": ["pd", "pw", "pm", "py"],
                        "description": "Past day / week / month / year.",
                    },
                },
                "required": ["query"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mode": {"type": "string"},
                    "result_count": {"type": "integer"},
                    "results": {"type": "array"},
                    "billing_units_actual": {"type": "integer"},
                },
                "required": ["query", "results", "billing_units_actual"],
            },
            "output_examples": [
                {
                    "input": {"query": "open source agent marketplace", "count": 5},
                    "output": {
                        "query": "open source agent marketplace",
                        "mode": "web",
                        "result_count": 3,
                        "results": [
                            {
                                "title": "Example — agent marketplace",
                                "url": "https://example.com",
                                "description": "A marketplace where AI agents hire each other.",
                                "age": None,
                                "site_name": "Example",
                                "thumbnail_url": None,
                            }
                        ],
                        "billing_units_actual": 1,
                    },
                }
            ],
        },
    ]
