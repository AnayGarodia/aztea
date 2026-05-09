"""Second chunk of built-in agent specs (argument to `specs.extend([...])`)."""

from __future__ import annotations

from typing import Any

from server.builtin_agents import pricing_overlay as _pricing_overlay
from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
)
from server.builtin_agents.constants import (
    DEPENDENCY_AUDITOR_AGENT_ID as _DEPENDENCY_AUDITOR_AGENT_ID,
)
from server.builtin_agents.constants import (
    DNS_INSPECTOR_AGENT_ID as _DNS_INSPECTOR_AGENT_ID,
)
from server.builtin_agents.constants import (
    PYTHON_EXECUTOR_AGENT_ID as _PYTHON_EXECUTOR_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def load_builtin_specs_part2() -> list[dict[str, Any]]:
    return [
        {
            "agent_id": _PYTHON_EXECUTOR_AGENT_ID,
            "name": "Python Code Executor",
            "description": "Use when the user wants to actually run Python code, not simulate it. Executes in a real sandboxed subprocess. Returns stdout, stderr, exit code, execution time, and an expert explanation of what the output means.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_PYTHON_EXECUTOR_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["code-execution", "python", "developer-tools", "compute"],
            "match_keywords": [
                "run python",
                "execute python",
                "python sandbox",
                "python repl",
                "evaluate python",
                "run script",
                "execute code",
            ],
            "block_keywords": [
                "javascript",
                "typescript",
                "node",
                "deno",
                "go ",
                "rust",
                "lint",
                "linter",
                "linting",
                "linnt",
                "jwt",
                "decode jwt",
                "jwt decode",
                "json web token",
                "joke",
                "jokes",
                "dinner",
                "credit card",
                "credit cards",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute",
                        "example": "print(sum(i**2 for i in range(10)))",
                    },
                    "stdin": {
                        "type": "string",
                        "default": "",
                        "description": "Optional input data fed to stdin",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 30,
                        "description": "Execution timeout in seconds",
                    },
                    "explain": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether to include an expert explanation of the output",
                    },
                },
                "required": ["code"],
            },
            "output_schema": _output_schema_object(
                {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "timed_out": {"type": "boolean"},
                    "execution_time_ms": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "variables_captured": {"type": "object"},
                },
                required=["stdout", "exit_code"],
            ),
            "variable_pricing": {
                "model": "per_unit",
                "field": "timeout",
                "field_type": "scalar",
                "unit_label": "second",
                "rate_usd": 0.00,
                "min_usd": 0.01,
            },
            "output_examples": [
                {
                    "input": {
                        "code": "import math\nresult = [math.factorial(n) for n in range(1, 11)]\nprint(result)",
                        "explain": True,
                    },
                    "output": {
                        "stdout": "[1, 2, 6, 24, 120, 720, 5040, 40320, 362880, 3628800]\n",
                        "stderr": "",
                        "exit_code": 0,
                        "timed_out": False,
                        "execution_time_ms": 28,
                        "explanation": "The code computes factorials 1! through 10! using a list comprehension over math.factorial. Output is correct — factorials grow rapidly and 10! = 3,628,800 as expected.",
                        "variables_captured": {
                            "result": [
                                1,
                                2,
                                6,
                                24,
                                120,
                                720,
                                5040,
                                40320,
                                362880,
                                3628800,
                            ]
                        },
                    },
                }
            ],
        },
        {
            "agent_id": str(_DNS_INSPECTOR_AGENT_ID),
            "name": "DNS & SSL Inspector",
            "description": "Use when the task requires checking domain health: DNS records, SSL certificate expiry, or HTTP security headers. Runs live checks against up to 10 domains and returns structured findings with actionable issues.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_DNS_INSPECTOR_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["dns", "ssl", "security", "infrastructure"],
            "match_keywords": [
                "dns",
                "ssl",
                "tls",
                "tls handshake",
                "ssl handshake",
                "certificate",
                "cert expiry",
                "expires",
                "expire",
                "expiring",
                "expiry",
                "domain expire",
                "domain expires",
                "domain expiry",
                "domain expiring",
                "domain about to expire",
                "domain about to",
                "is this domain",
                "check this domain",
                "check the domain",
                "hsts",
                "csp",
                "csp header",
                "csp headers",
                "content security policy",
                "dkim",
                "spf",
                "dmarc",
                "mx record",
                "subdomain takeover",
                "dangling cname",
                "hsts preload",
                "security headers",
                "security header",
                "http header",
                "http headers",
                "ssl issue",
                "ssl issues",
                "ssl problem",
                "ssl problems",
            ],
            "kind": "aztea_built",
            "category": "Security",
            "is_featured": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Domain names to inspect (max 10)",
                    },
                    "checks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["dns", "ssl", "http"],
                        "description": "Checks to run: dns, ssl, http, mx",
                    },
                },
                "required": ["domains"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "results": {"type": "array"},
                    "billing_units_actual": {"type": "integer"},
                },
            },
            "variable_pricing": {
                "model": "tiered",
                "field": "domains",
                "field_type": "array",
                "unit_label": "domain",
                "tiers": [
                    {"max_units": 1, "price_usd": 0.01},
                    {"max_units": 3, "price_usd": 0.03},
                    {"max_units": 10, "price_usd": 0.08},
                ],
            },
            "output_examples": [
                {
                    "input": {"domains": ["example.com"], "checks": ["dns", "ssl"]},
                    "output": {
                        "results": [
                            {
                                "domain": "example.com",
                                "dns": {
                                    "a": ["93.184.216.34"],
                                    "mx": [],
                                    "ns": ["a.iana-servers.net"],
                                },
                                "ssl": {
                                    "valid": True,
                                    "expires_in_days": 180,
                                    "issuer": "DigiCert Inc",
                                    "subject": "example.com",
                                },
                                "issues": [],
                            }
                        ],
                        "billing_units_actual": 1,
                    },
                }
            ],
        },
        # ── Dependency Auditor ───────────────────────────────────────────────────
        {
            "agent_id": _DEPENDENCY_AUDITOR_AGENT_ID,
            "name": "Dependency Auditor",
            "description": "Use this when the user wants to audit their dependencies for security vulnerabilities, outdated packages, or license issues. Accepts the text of a package.json or requirements.txt and returns a structured report: CVEs per package, license risks, and prioritized upgrade recommendations.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_DEPENDENCY_AUDITOR_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": [
                "security",
                "cve",
                "dependencies",
                "npm",
                "pypi",
                "developer-tools",
            ],
            "match_keywords": [
                "vulnerabilities",
                "vulnerability",
                "package.json",
                "requirements.txt",
                "audit",
                "auditing",
                "dependencies",
                "dependency",
                "is this dependency",
                "is this package",
                "package safe",
                "package dangerous",
                "dependency safe",
                "dependency dangerous",
                "outdated packages",
                "outdated",
                "license risk",
                "license issue",
                "supply chain",
                "sbom",
                "software bill of materials",
                "npm audit",
                "pip audit",
                "yarn audit",
                "audit my dependencies",
                "audit my deps",
                "audit my project",
                "audit a python",
                "audit my python",
                "audit this manifest",
                "depndency",
                "dependancy",
            ],
            "block_keywords": [
                "owasp",
                "owasp top 10",
                "jwt",
                "decode jwt",
                "joke",
                "dinner",
                "credit card",
                "credit cards",
            ],
            "kind": "aztea_built",
            "category": "Security",
            "examples_sensitive": True,
            "is_featured": True,
            "input_schema": _output_schema_object(
                {
                    "manifest": {
                        "type": "string",
                        "title": "Package Manifest",
                        "description": "Contents of package.json or requirements.txt (paste the full file).",
                        "maxLength": 10000,
                    },
                    "ecosystem": {
                        "type": "string",
                        "title": "Ecosystem",
                        # Schema accepts any string so unsupported ecosystems
                        # (maven, cargo, gradle, gomod, ...) reach the runtime
                        # which returns a structured error pointing the
                        # caller at the right tool (refunds automatically).
                        "description": (
                            "Package ecosystem. Supported: npm, pypi, auto. "
                            "Other ecosystems (maven, cargo, gradle, gomod) "
                            "return a clear unsupported_ecosystem error."
                        ),
                        "default": "auto",
                    },
                    "checks": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["cve", "outdated", "license"],
                        },
                        "title": "Checks",
                        "description": "Which checks to run. Defaults to all three.",
                        "default": ["cve", "outdated", "license"],
                    },
                },
                required=["manifest"],
            ),
            "output_schema": _output_schema_object(
                {
                    "ecosystem": {"type": "string"},
                    "total_packages": {"type": "integer"},
                    "vulnerable_count": {"type": "integer"},
                    "outdated_count": {"type": "integer"},
                    "critical_count": {"type": "integer"},
                    "packages": {"type": "array", "items": {"type": "object"}},
                    "top_priorities": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                },
                required=["ecosystem", "total_packages", "packages", "summary"],
            ),
            "output_examples": [
                {
                    "input": {
                        "manifest": '{"dependencies": {"lodash": "4.17.20"}}',
                        "ecosystem": "npm",
                    },
                    "output": {
                        "ecosystem": "npm",
                        "total_packages": 1,
                        "vulnerable_count": 1,
                        "outdated_count": 1,
                        "critical_count": 1,
                        "packages": [
                            {
                                "name": "lodash",
                                "current_version": "4.17.20",
                                "latest_version": "4.17.21",
                                "cves": [
                                    {
                                        "id": "CVE-2021-23337",
                                        "severity": "high",
                                        "description": "Command injection via template",
                                        "fixed_in": "4.17.21",
                                    }
                                ],
                                "license": "MIT",
                                "license_risk": "none",
                                "action": "upgrade",
                            }
                        ],
                        "top_priorities": [
                            "Upgrade lodash to 4.17.21 (CVE-2021-23337)"
                        ],
                        "summary": "1 package with a high-severity CVE. Upgrade lodash immediately.",
                    },
                }
            ],
        },
    ]


def _assert_prices_match_overlay(specs: list[dict[str, Any]]) -> None:
    """Raise at import time if a spec's price_per_call_usd drifts below its overlay minimum.

    The overlay is the canonical pricing source for variable-priced agents.
    A spec price BELOW the overlay minimum means callers see an inaccurate
    price in discovery — catch this at startup, not at runtime.
    """
    overlay = _pricing_overlay.get_pricing_overlay()
    for spec in specs:
        agent_id = spec.get("agent_id", "")
        if agent_id not in overlay:
            continue
        config = overlay[agent_id].get("pricing_config", {})
        min_cents = config.get("min_cents", 0)
        min_usd = min_cents / 100
        spec_price = float(spec.get("price_per_call_usd", 0))
        if spec_price < min_usd - 0.0001:  # 0.1-cent tolerance for float arithmetic
            raise RuntimeError(
                f"Price drift detected for agent {agent_id!r}: "
                f"spec price ${spec_price:.4f} < overlay minimum ${min_usd:.4f}. "
                "Update pricing_overlay.py or the spec to match."
            )


# Eagerly validate on import so startup fails fast on price drift.
_assert_prices_match_overlay(load_builtin_specs_part2())
