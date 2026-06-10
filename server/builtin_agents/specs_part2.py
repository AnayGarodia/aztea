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
from server.builtin_agents.constants import (
    QUANT_PATCH_VALIDATOR_AGENT_ID as _QUANT_PATCH_VALIDATOR_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def load_builtin_specs_part2() -> list[dict[str, Any]]:
    return [
        {
            "agent_id": _PYTHON_EXECUTOR_AGENT_ID,
            "name": "Python Code Executor",
            "description": (
                "Use when the user wants to actually run Python code, not "
                "simulate it. Executes in a real sandboxed subprocess and "
                "returns stdout, stderr, exit code, execution time, and an "
                "expert explanation. Runtime constraints (enforced; calls "
                "that exceed them are rejected, not silently truncated): "
                "128 MB memory cap with a 32 MB pre-spawn static-allocation "
                "guard; 30 s hard timeout (the sync /call gateway has an "
                "8 s wall budget — use manage_workflow(hire_async) for "
                "longer jobs, which gets a 10-minute async budget); "
                "no pip install; no subprocess spawning or shell execution; "
                "no arbitrary file writes outside the sandbox tempdir; no "
                "network sockets except via the agent contract. Stdlib is "
                "fully available. Third-party libs are whatever the "
                "executor image bundles (e.g. requests, numpy, pandas)."
            ),
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
                        "description": (
                            "Python code to execute. Sandboxed: no pip "
                            "install, no subprocess spawning, no shell "
                            "execution, no arbitrary file writes, 128 MB "
                            "memory cap. Stdlib only, plus whatever the "
                            "executor image preinstalls (requests, numpy, "
                            "pandas are typical)."
                        ),
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
                        "description": (
                            "Execution timeout in seconds. Hard cap is 30; "
                            "anything above is rejected. The sync /call "
                            "gateway adds a separate 8 s wall budget — "
                            "callers needing more than 8 s should use "
                            "manage_workflow(hire_async), which honors a "
                            "10-minute async wall budget."
                        ),
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
                    "explanation_status": {
                        "type": "string",
                        "enum": [
                            "ok",
                            "disabled",
                            "skipped_timeout",
                            "skipped_no_output",
                            "provider_failed",
                        ],
                        "description": "Why `explanation` is or is not present",
                    },
                    "code_submitted": {
                        "type": "string",
                        "description": (
                            "Truncated echo of the submitted code, present "
                            "only when timed_out=true so hangs are debuggable"
                        ),
                    },
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
                    "packages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "description": (
                                "Per-package audit row. ``cvss`` may be null "
                                "when the upstream advisory only ships a "
                                "severity label (HIGH/CRITICAL) without a "
                                "numeric base score — severity carries the "
                                "label in that case. ``action`` is one of: "
                                "upgrade, replace, review, ok, not_found, "
                                "version_unreachable. ``notes`` explains "
                                "non-CVE actions."
                            ),
                        },
                    },
                    "top_priorities": {"type": "array", "items": {"type": "string"}},
                    "parse_warnings": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Manifest lines that were not audited, with a "
                            "classified ``reason``: editable_not_audited "
                            "(-e installs), vcs_url_not_audited (git+/URL "
                            "specs), nested_requirements_not_followed "
                            "(-r/-c includes), pip_option_ignored "
                            "(--index-url etc.), unparseable, or "
                            "duplicate_entry (merged). Extras "
                            "(``pkg[socks]``), env markers, and npm "
                            "prerelease versions parse correctly and do "
                            "not warn."
                        ),
                    },
                    "summary": {"type": "string"},
                },
                required=[
                    "ecosystem", "total_packages", "packages",
                    "parse_warnings", "summary",
                ],
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
                                        "cvss": 7.2,
                                        "description": "Command injection via template",
                                        "fixed_in": "4.17.21",
                                    }
                                ],
                                "license": "MIT",
                                "license_risk": "none",
                                "action": "upgrade",
                                "notes": None,
                            }
                        ],
                        "top_priorities": [
                            "Upgrade lodash to 4.17.21 (CVE-2021-23337)"
                        ],
                        "parse_warnings": [],
                        "summary": "1 package with a high-severity CVE. Upgrade lodash immediately.",
                    },
                }
            ],
        },
        {
            "agent_id": _QUANT_PATCH_VALIDATOR_AGENT_ID,
            "name": "Quant Patch Validator",
            "description": (
                "Differential fuzzer for AI-written quant code. Validates "
                "an LLM-generated patch against the original (reference) "
                "implementation by driving both with Hypothesis-generated "
                "inputs, then clusters and triages divergences as "
                "expected-fix vs unintended-regression vs both-wrong. "
                "Use when an AI suggested a change to numerical or "
                "trading-logic code and you need to confirm it doesn't "
                "silently introduce off-by-one, sign-flip, unit, "
                "annualization, or contract-shape bugs. Returns "
                "confirmed regressions with reproducible inputs, "
                "verified invariants, and full fuzz statistics. "
                "Candidate code runs in an isolated subprocess "
                "(per-call SIGKILL on 2.5s timeout; per-Harness tempdir "
                "cwd contains relative-path FS writes and sys.path "
                "mutations). Caller-tunable rtol/atol; default tolerances "
                "are calibrated to typical AI failure magnitudes; "
                "precision 1.0 / recall 1.0 / false-alarm 0.0 on the v0.1 "
                "quant-bench corpus. Validated patches earn a workspace-"
                "sealed audit trail when called with _workspace_id. "
                "Sensitive inputs are never replayed as public work "
                "examples (examples_sensitive=True). Priced for quant CI "
                "use ($1.50 covers ~5000 fuzz iterations + LLM triage); "
                "not optimized for casual exploration — see the runbook "
                "for the v1 containment surface and the v0.2 plan to "
                "close residual absolute-path / network gaps via "
                "live_sandbox."
            ),
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_QUANT_PATCH_VALIDATOR_AGENT_ID],
            "price_per_call_usd": 1.50,
            "tags": ["code-quality", "fuzzing", "quant", "ai-validation", "testing"],
            "category": "Code Quality",
            "examples_sensitive": True,
            "cacheable": False,
            "runtime_requirements": ["python>=3.10", "numpy", "pandas", "hypothesis"],
            "tooling_kind": "fuzzer",
            "is_featured": True,
            "input_schema": {
                "type": "object",
                "required": ["reference_code", "candidate_code"],
                "properties": {
                    "reference_code": {
                        "type": "string",
                        "description": "Pre-patch / reference implementation source.",
                    },
                    "candidate_code": {
                        "type": "string",
                        "description": "AI-generated candidate to validate.",
                    },
                    "fuzz_budget": {
                        "type": "string",
                        "enum": ["quick", "standard", "deep"],
                        "default": "standard",
                        "description": (
                            "Runtime budget. quick=30s, standard=5min, "
                            "deep=30min. Affects how many inputs the fuzzer "
                            "tries; does not affect price in v1."
                        ),
                    },
                    "fuzz_seconds": {
                        "type": "number",
                        "description": (
                            "Optional exact wall-clock budget (seconds), "
                            "capped at the chosen tier's nominal budget. "
                            "Use for low-latency sync callers (the sync "
                            "gateway has an 8 s budget; choose 6 or below)."
                        ),
                    },
                    "fuzz_engine": {
                        "type": "string",
                        "enum": ["hypothesis", "atheris"],
                        "default": "hypothesis",
                        "description": (
                            "Fuzzer backend. atheris is coverage-guided but "
                            "requires libFuzzer (Linux + clang); on macOS "
                            "falls back to hypothesis."
                        ),
                    },
                    "rtol": {
                        "type": "number",
                        "default": 1e-5,
                        "description": "Relative tolerance for numerical equality.",
                    },
                    "atol": {
                        "type": "number",
                        "default": 1e-7,
                        "description": "Absolute tolerance floor for numerical equality.",
                    },
                    "spec_hint": {
                        "type": "string",
                        "description": (
                            "Optional natural-language description of what "
                            "the patch is intended to change. Without it, "
                            "every divergence is treated as a regression."
                        ),
                    },
                    "auto_tune_tolerance": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Run the reference twice on permuted inputs and "
                            "set atol from observed self-divergence. Use only "
                            "for STATELESS functions (mean, var) — for time-"
                            "ordered functions (RSI, rolling stats) this will "
                            "over-tolerate."
                        ),
                    },
                    "track_coverage": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Wrap fuzzing with coverage.py to report what "
                            "fraction of the candidate's branches were "
                            "exercised. Adds modest overhead; off by default."
                        ),
                    },
                },
            },
            "output_schema": _output_schema_object(
                {
                    "verdict": {
                        "type": "string",
                        "enum": [
                            "equivalent",
                            "regressions_found",
                            "contract_broken",
                            "signature_divergence",
                            "intended_changes_only",
                        ],
                    },
                    "signature": {"type": ["object", "null"]},
                    "signature_divergence": {"type": ["object", "null"]},
                    "confirmed_regressions": {"type": "array"},
                    "expected_divergences": {"type": "array"},
                    "fuzz_stats": {"type": "object"},
                    "spec_hint_used": {"type": "boolean"},
                },
                required=["verdict", "fuzz_stats"],
            ),
            "output_examples": [
                {
                    "input": {
                        "reference_code": (
                            "import numpy as np\n"
                            "def f(prices, window):\n"
                            "    out = np.full(prices.shape, np.nan)\n"
                            "    for i in range(window, prices.size):\n"
                            "        out[i] = prices[i-window:i].mean()\n"
                            "    return out\n"
                        ),
                        "candidate_code": (
                            "import numpy as np\n"
                            "def f(prices, window):\n"
                            "    out = np.full(prices.shape, np.nan)\n"
                            "    # bug: includes today's bar (lookahead)\n"
                            "    for i in range(window-1, prices.size):\n"
                            "        out[i] = prices[i-window+1:i+1].mean()\n"
                            "    return out\n"
                        ),
                        "fuzz_budget": "quick",
                    },
                    "output": {
                        "verdict": "regressions_found",
                        "signature_divergence": None,
                        "confirmed_regressions": [
                            {
                                "cluster_id": "C001",
                                "divergence_kind": "value",
                                "member_count": 142,
                                "verdict": "regression",
                                "hypothesis": (
                                    "Numerical divergence at moderate magnitude. "
                                    "Window shifted by one — looks like a "
                                    "lookahead-by-one bug."
                                ),
                                "confidence": 0.85,
                            }
                        ],
                        "fuzz_stats": {
                            "tier_used": "quick",
                            "inputs_explored": 14820,
                            "divergences_found": 142,
                            "clusters": 1,
                        },
                    },
                },
                {
                    "input": {
                        "reference_code": "def f(x): return x*2\n",
                        "candidate_code": "def f(y): return y*2\n",
                        "fuzz_budget": "quick",
                    },
                    "output": {
                        "verdict": "equivalent",
                        "confirmed_regressions": [],
                        "fuzz_stats": {"clusters": 0},
                    },
                },
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
