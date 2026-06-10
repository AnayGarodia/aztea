"""Twelfth chunk of built-in agent specs — the agent-readable-web wedge.

Phase 0 of the agent-readable-web build plan: site_navigator. Goal-directed
navigation over the accessibility tree (token-cheap vs screenshots), returning
structured data plus a reusable site map. Phase 1 will sign + share that map
via the commons; this file ships the standalone magnet.
"""

from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
)
from server.builtin_agents.constants import (
    SITE_NAVIGATOR_AGENT_ID as _SITE_NAVIGATOR_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def load_builtin_specs_part12() -> list[dict[str, Any]]:
    """Built-in specs for the agent-readable-web wedge (currently: site_navigator)."""
    return [
        {
            "agent_id": _SITE_NAVIGATOR_AGENT_ID,
            "name": "Web Agent",
            "description": (
                "One agent that READS and ACTS on the live web. Default (no 'action'): "
                "give it a URL and a plain-English goal (e.g. 'list the pricing tiers'); "
                "it renders with headless Chromium, reads the accessibility tree (far "
                "cheaper than a screenshot), and returns the structured answer plus a "
                "reusable site map — or clean LLM-ready markdown / HTML / links via the "
                "'formats' option. Static/SSR pages and sites with a discoverable JSON "
                "API skip the browser entirely. Set 'action' to interact / preview / "
                "dry_run / commit to perform a bounded action (click, fill, select, "
                "scroll, log in) under a signed, capped, single-use mandate. The action "
                "(write) side is FAIL-CLOSED: it does nothing until an operator enables "
                "AZTEA_ACTION_WEB_ENABLED, so a default deploy only reads. If no LLM is "
                "configured, reads degrade to returning the retrieved data."
            ),
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SITE_NAVIGATOR_AGENT_ID],
            # Phase 0 price. Phase 1 (commons royalties) revisits this per the
            # plan's T2 amendment — a royalty carved from the platform fee needs
            # a non-trivial fee, so the base price rises when reuse ships.
            "price_per_call_usd": 0.05,
            "tags": [
                "web",
                "navigation",
                "extraction",
                "accessibility-tree",
                "structured-data",
                "playwright",
            ],
            "match_keywords": [
                "extract structured data",
                "extract data from",
                "get data from website",
                "get the pricing from",
                "pull data from",
                "pull the pricing",
                "read the page and",
                "what does the page say",
                "find on the website",
                "navigate the site",
                "navigate to find",
                "answer from the page",
                "structured extraction",
                "extract the table from",
                "list the items on",
                "get structured data from url",
            ],
            "kind": "aztea_built",
            "category": "Web",
            "is_featured": True,
            "internal_only": False,
            # Identical (url, goal) repeats are free via core/cache; the richer
            # URL-keyed map reuse arrives with the Phase 1 commons.
            "cacheable": True,
            # Caller goals and page content can carry PII; never replay inputs
            # into public work examples (defense-in-depth per CLAUDE.md privacy gates).
            "examples_sensitive": True,
            "runtime_requirements": [
                "playwright",
                "chromium (skipped when http-first or API-spec replay serves the call)",
                "trafilatura + markdownify (optional; for the 'markdown' format)",
                "llm provider optional for synthesis",
            ],
            "tooling_kind": "browser_automation",
            "stability_tier": "beta",
            "codex_recommended": True,
            "short_use_cases": [
                "get the pricing tiers from a page",
                "extract structured data from any site by goal",
                "map the links/forms an agent can act on",
            ],
            "input_schema": _output_schema_object(
                {
                    "url": {
                        "type": "string",
                        "title": "URL",
                        "description": "Public https:// URL to navigate. SSRF-blocked.",
                    },
                    "goal": {
                        "type": "string",
                        "title": "Goal",
                        "description": (
                            "Plain-English description of the data you want, e.g. "
                            "'list the pricing tiers'. Required when 'structured' is in "
                            "formats (the default); optional for a markdown/html/links scrape."
                        ),
                    },
                    "formats": {
                        "type": "array",
                        "title": "Output formats",
                        "description": (
                            "Any of: 'structured' (goal-directed JSON, default), 'markdown' "
                            "(clean LLM-ready markdown), 'html' (rendered HTML), 'links'."
                        ),
                        "items": {
                            "type": "string",
                            "enum": ["structured", "markdown", "html", "links"],
                        },
                        "default": ["structured"],
                    },
                    "schema": {
                        "type": "object",
                        "title": "Extraction schema",
                        "description": (
                            "Optional JSON Schema (the /extract mode). When present, the "
                            "structured result is validated against it, retried once on a "
                            "mismatch, and returns a typed _extraction_failed marker if it "
                            "still doesn't conform — Firecrawl-style schema extraction."
                        ),
                    },
                    "wait_for": {
                        "type": "string",
                        "title": "Wait condition",
                        "description": "CSS selector or 'networkidle' (default).",
                        "default": "networkidle",
                    },
                    "wait_ms": {
                        "type": "integer",
                        "title": "Extra wait (ms)",
                        "description": (
                            "Additional settle wait after load (max 6000 ms). The "
                            "sync /call gateway has an 8 s budget and this agent "
                            "also runs an LLM resolve, so callers should use "
                            "manage_workflow(action='hire_async') or POST /jobs, "
                            "which honor a 20-minute async budget."
                        ),
                        "default": 1500,
                        "maximum": 6000,
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "title": "Force refresh",
                        "description": (
                            "Skip the no-browser API-spec replay and force a fresh "
                            "render (use when you suspect the cached API spec is stale)."
                        ),
                        "default": False,
                    },
                    "action": {
                        "type": "string",
                        "title": "Action (write web)",
                        "description": (
                            "Omit to READ. Set to interact / preview / dry_run / commit "
                            "to ACT on the page. The write side is OFF until an operator "
                            "enables AZTEA_ACTION_WEB_ENABLED (commit + credentials need "
                            "their own flags)."
                        ),
                        "enum": ["interact", "preview", "dry_run", "commit"],
                    },
                    "steps": {
                        "type": "array",
                        "title": "Interaction steps",
                        "description": "interact / dry_run: bounded sequence (max 10) of {action: click|fill|select|scroll|wait, target, value}.",
                        "items": {"type": "object"},
                    },
                    "mandate_id": {
                        "type": "string",
                        "title": "Mandate ID",
                        "description": "preview / dry_run / commit: a signed action mandate (spend cap, allowed domains, expiry, single-use nonce).",
                    },
                    "confirmation_nonce": {
                        "type": "string",
                        "title": "Confirmation nonce",
                        "description": "commit only: the mandate's single-use nonce.",
                    },
                    "use_credential": {
                        "type": "string",
                        "title": "Use stored credential",
                        "description": "dry_run only: 'password' | 'cookies' | 'totp' to log into the mandate owner's account first. Gated by AZTEA_CREDENTIAL_INJECTION_ENABLED.",
                        "enum": ["password", "cookies", "totp"],
                    },
                },
                required=["url"],
            ),
            "output_schema": _output_schema_object(
                {
                    "url": {"type": "string"},
                    "requested_url": {"type": "string"},
                    "goal": {"type": "string"},
                    "result": {
                        "description": (
                            "Structured answer to the goal (object or array), or "
                            "null when no LLM provider is configured (degraded mode)."
                        ),
                    },
                    "site_map": {
                        "type": "object",
                        "description": (
                            "Reusable affordance map: links, buttons, inputs, "
                            "headings, plus a structural dom_fingerprint."
                        ),
                        "properties": {
                            "final_url": {"type": "string"},
                            "title": {"type": "string"},
                            "affordances": {"type": "object"},
                            "node_count": {"type": "integer"},
                            "dom_fingerprint": {"type": "string"},
                            "graph": {
                                "type": "object",
                                "description": "navigation graph: sections by heading + entry-point links.",
                            },
                            "schema": {"type": "string"},
                        },
                    },
                    "source": {
                        "type": "string",
                        "description": "which path served the call: fresh | http_first | api_spec.",
                    },
                    "reuse": {
                        "type": "object",
                        "description": "commons coverage signal (commons_map_available / _id / _author_did).",
                    },
                    "modality_used": {
                        "type": "string",
                        "description": "accessibility_tree | http_first | api_spec.",
                    },
                    "cost_class": {
                        "type": "string",
                        "description": "'cheap' when a no-browser path served it, else 'expensive'.",
                    },
                    "markdown": {
                        "type": "string",
                        "description": "clean LLM-ready markdown — present only when 'markdown' is in formats.",
                    },
                    "html": {
                        "type": "string",
                        "description": "rendered HTML — present only when 'html' is in formats.",
                    },
                    "links": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "page links — present only when 'links' is in formats.",
                    },
                    "modality_recommended": {
                        "type": "string",
                        "description": "'screenshot' when the a11y tree was too sparse and a vision pass would help.",
                    },
                    "observation_receipt": {
                        "type": "object",
                        "description": "signed proof-of-observation (provenance, not truth) — verifiable offline via the agent did:web key.",
                    },
                    "execution_time_ms": {"type": "integer"},
                    "llm_used": {"type": "boolean"},
                    "degraded_mode": {"type": "boolean"},
                },
                required=["url", "goal", "site_map"],
            ),
            "output_examples": [
                {
                    "input": {
                        "url": "https://example.com/pricing",
                        "goal": "list the pricing tiers and their monthly price",
                    },
                    "output": {
                        "url": "https://example.com/pricing",
                        "requested_url": "https://example.com/pricing",
                        "goal": "list the pricing tiers and their monthly price",
                        "result": {
                            "tiers": [
                                {"name": "Starter", "price_monthly_usd": 0},
                                {"name": "Pro", "price_monthly_usd": 20},
                            ]
                        },
                        "site_map": {
                            "final_url": "https://example.com/pricing",
                            "title": "Pricing",
                            "affordances": {
                                "links": ["Docs", "Sign up"],
                                "buttons": ["Start free"],
                                "inputs": [],
                                "headings": ["Pricing", "Starter", "Pro"],
                            },
                            "node_count": 84,
                            "dom_fingerprint": "3f1a…",
                            "schema": "aztea/site-map/2",
                        },
                        "source": "fresh",
                        "reuse": {"reused": False, "source": "fresh"},
                        "modality_used": "accessibility_tree",
                        "cost_class": "expensive",
                        "execution_time_ms": 3400,
                        "llm_used": True,
                        "degraded_mode": False,
                    },
                }
            ],
        },
    ]
