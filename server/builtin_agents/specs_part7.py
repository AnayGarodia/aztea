"""Seventh chunk of built-in agent specs — YC launch agents added 2026-05-09.

Registers five high-impact specialists: docs_grounder, sast_scanner,
stripe_webhook_debugger, load_tester, and ci_failure_reproducer.
"""

from __future__ import annotations

from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS,
    CI_FAILURE_REPRODUCER_AGENT_ID,
    COVERAGE_RUNNER_AGENT_ID,
    DOCKERFILE_ANALYZER_AGENT_ID,
    DOCS_GROUNDER_AGENT_ID,
    LOAD_TESTER_AGENT_ID,
    OPENAPI_VALIDATOR_AGENT_ID,
    SAST_SCANNER_AGENT_ID,
    STRIPE_WEBHOOK_DEBUGGER_AGENT_ID,
)


def load_builtin_specs_part7() -> list[dict]:
    return [
        # ------------------------------------------------------------------ #
        # Docs Grounder — live documentation fetcher                          #
        # ------------------------------------------------------------------ #
        {
            "agent_id": DOCS_GROUNDER_AGENT_ID,
            "name": "Docs Grounder",
            "slug": "docs-grounder",
            "description": (
                "Fetches current official documentation for any library or framework and "
                "returns API signatures, code examples, migration notes, and gotchas — "
                "with citations. Eliminates hallucinated APIs from stale training data."
            ),
            "endpoint_url": BUILTIN_INTERNAL_ENDPOINTS[DOCS_GROUNDER_AGENT_ID],
            "price_per_call_usd": 0.02,
            "tags": ["documentation", "research", "developer-tools", "live-data"],
            "is_featured": True,
            "cacheable": True,
            "category": "Research",
            "runtime_requirements": ["web_search agent", "httpx"],
            "tooling_kind": "live_fetch_plus_llm",
            "stability_tier": "beta",
            "codex_recommended": True,
            "short_use_cases": [
                "look up current Stripe API",
                "find Next.js 14 migration notes",
                "get Prisma schema syntax",
            ],
            "match_keywords": [
                "documentation", "docs", "api reference", "changelog",
                "migration guide", "upgrade guide", "how to use",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "library": {
                        "type": "string",
                        "description": "Library or framework name, e.g. 'stripe', 'nextjs', 'react', 'prisma'",
                    },
                    "question": {
                        "type": "string",
                        "description": "Specific question or topic, e.g. 'how do webhook signatures work'",
                    },
                    "version": {
                        "type": "string",
                        "description": "Version to target, e.g. 'latest', '13.4', 'v4'",
                        "default": "latest",
                    },
                },
                "required": ["library"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "library": {"type": "string"},
                    "version_found": {"type": "string"},
                    "summary": {"type": "string"},
                    "code_example": {"type": "string"},
                    "api_signatures": {"type": "array", "items": {"type": "string"}},
                    "gotchas": {"type": "array", "items": {"type": "string"}},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "title": {"type": "string"},
                                "excerpt": {"type": "string"},
                            },
                        },
                    },
                    "as_of_date": {"type": "string"},
                    "query_used": {"type": "string"},
                },
                "required": ["library", "summary", "sources"],
            },
            "output_examples": [
                {
                    "input": {"library": "stripe", "question": "how do webhook signatures work"},
                    "output": {
                        "library": "stripe",
                        "version_found": "latest",
                        "summary": "Stripe signs webhook events with HMAC-SHA256. You must verify the signature before parsing the JSON body — parsing first is a common security bug.",
                        "code_example": "import stripe\nevent = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)",
                        "api_signatures": ["stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)"],
                        "gotchas": ["Parse raw body before JSON decode", "Use raw bytes not string for signature"],
                        "sources": [{"url": "https://stripe.com/docs/webhooks/signatures", "title": "Webhook signatures", "excerpt": "..."}],
                        "as_of_date": "2026-05-09",
                        "query_used": "stripe webhook signature verification documentation",
                    },
                }
            ],
        },
        # ------------------------------------------------------------------ #
        # SAST Scanner — static application security testing                  #
        # ------------------------------------------------------------------ #
        {
            "agent_id": SAST_SCANNER_AGENT_ID,
            "name": "SAST Scanner",
            "slug": "sast-scanner",
            "description": (
                "Runs semgrep and bandit on submitted code files and returns structured "
                "security findings with severity, CWE, rule ID, and fix hints. "
                "Claude can suggest security issues; this agent finds them by running real tools."
            ),
            "endpoint_url": BUILTIN_INTERNAL_ENDPOINTS[SAST_SCANNER_AGENT_ID],
            "price_per_call_usd": 0.04,
            "tags": ["security", "sast", "static-analysis", "code-review"],
            "is_featured": True,
            "cacheable": True,
            "category": "Security",
            "runtime_requirements": ["semgrep (optional)", "bandit (optional, Python only)"],
            "tooling_kind": "tool_execution",
            "stability_tier": "beta",
            "codex_recommended": True,
            "short_use_cases": [
                "scan Python for injection flaws",
                "find SQL injection in JS",
                "pre-commit security check",
            ],
            "match_keywords": [
                "security scan", "sast", "static analysis", "semgrep", "bandit",
                "injection", "xss", "vulnerability", "security audit",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["name", "content"],
                        },
                        "description": "Code files to scan (max 20 files, 100 KB total)",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["auto", "python", "javascript", "typescript", "go", "java"],
                        "default": "auto",
                    },
                },
                "required": ["files"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "findings": {"type": "array", "items": {"type": "object"}},
                    "total_findings": {"type": "integer"},
                    "by_severity": {"type": "object"},
                    "files_scanned": {"type": "integer"},
                    "languages_detected": {"type": "array", "items": {"type": "string"}},
                    "tools_used": {"type": "array", "items": {"type": "string"}},
                    "scan_time_ms": {"type": "integer"},
                },
                "required": ["findings", "total_findings", "by_severity"],
            },
            "output_examples": [
                {
                    "input": {
                        "files": [{"name": "app.py", "content": "import subprocess\nsubprocess.run(user_input, shell=True)"}],
                        "language": "python",
                    },
                    "output": {
                        "findings": [
                            {
                                "file": "app.py",
                                "line": 2,
                                "column": 0,
                                "severity": "high",
                                "rule_id": "python.lang.security.audit.subprocess-shell-true",
                                "cwe": "CWE-78",
                                "message": "subprocess call with shell=True is a security risk",
                                "code_snippet": "subprocess.run(user_input, shell=True)",
                                "fix_hint": "Pass arguments as a list and set shell=False",
                                "tool": "semgrep",
                            }
                        ],
                        "total_findings": 1,
                        "by_severity": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
                        "files_scanned": 1,
                        "languages_detected": ["python"],
                        "tools_used": ["semgrep", "bandit"],
                        "scan_time_ms": 1200,
                    },
                }
            ],
        },
        # ------------------------------------------------------------------ #
        # Stripe Webhook Debugger                                              #
        # ------------------------------------------------------------------ #
        {
            "agent_id": STRIPE_WEBHOOK_DEBUGGER_AGENT_ID,
            "name": "Stripe Webhook Debugger",
            "slug": "stripe-webhook-debugger",
            "description": (
                "Sends real Stripe-signed test webhook events to your endpoint and verifies "
                "correct behavior: signature verification, idempotency, status codes, and common "
                "bugs. No Stripe API key needed — constructs signed events locally."
            ),
            "endpoint_url": BUILTIN_INTERNAL_ENDPOINTS[STRIPE_WEBHOOK_DEBUGGER_AGENT_ID],
            "price_per_call_usd": 0.03,
            "tags": ["stripe", "payments", "webhooks", "testing", "developer-tools"],
            "is_featured": True,
            "cacheable": False,
            "category": "Developer Tools",
            "runtime_requirements": ["requests", "hmac (stdlib)"],
            "tooling_kind": "live_network_checks",
            "stability_tier": "beta",
            "codex_recommended": True,
            "short_use_cases": [
                "debug Stripe webhook handler",
                "test checkout.session.completed",
                "verify signature validation",
            ],
            "match_keywords": [
                "stripe", "webhook", "payment", "checkout", "subscription",
                "invoice", "billing", "stripe event",
            ],
            "examples_sensitive": False,
            "input_schema": {
                "type": "object",
                "properties": {
                    "endpoint_url": {
                        "type": "string",
                        "description": "Your webhook handler URL, e.g. http://localhost:3000/webhooks/stripe",
                    },
                    "webhook_secret": {
                        "type": "string",
                        "description": "Stripe webhook signing secret (whsec_...)",
                    },
                    "event_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Event types to test. Defaults to common checkout/subscription events.",
                        "default": ["checkout.session.completed", "customer.subscription.updated", "invoice.payment_failed"],
                    },
                    "timeout_seconds": {"type": "integer", "default": 10, "maximum": 30},
                },
                "required": ["endpoint_url", "webhook_secret"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "endpoint_url": {"type": "string"},
                    "tests_run": {"type": "integer"},
                    "passed": {"type": "integer"},
                    "failed": {"type": "integer"},
                    "results": {"type": "array", "items": {"type": "object"}},
                    "common_issues_detected": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                },
                "required": ["tests_run", "passed", "failed", "results"],
            },
            "output_examples": [
                {
                    "input": {
                        "endpoint_url": "http://localhost:3000/webhooks/stripe",
                        "webhook_secret": "whsec_test_abc123",
                        "event_types": ["checkout.session.completed"],
                    },
                    "output": {
                        "endpoint_url": "http://localhost:3000/webhooks/stripe",
                        "tests_run": 3,
                        "passed": 2,
                        "failed": 1,
                        "results": [
                            {
                                "test_name": "checkout.session.completed — valid signature",
                                "event_type": "checkout.session.completed",
                                "status": "pass",
                                "http_status": 200,
                                "response_time_ms": 45,
                                "failure_reason": "",
                                "diagnosis": "",
                            },
                            {
                                "test_name": "checkout.session.completed — invalid signature",
                                "event_type": "checkout.session.completed",
                                "status": "fail",
                                "http_status": 200,
                                "response_time_ms": 40,
                                "failure_reason": "Handler returned 200 for an invalid signature",
                                "diagnosis": "Your handler is not verifying the Stripe-Signature header. Anyone can send fake events.",
                            },
                        ],
                        "common_issues_detected": ["Signature not verified — handler accepts forged events"],
                        "summary": "2/3 tests passed. Critical: handler does not verify webhook signatures.",
                    },
                }
            ],
        },
        # ------------------------------------------------------------------ #
        # Load Tester                                                          #
        # ------------------------------------------------------------------ #
        {
            "agent_id": LOAD_TESTER_AGENT_ID,
            "name": "Load Tester",
            "slug": "load-tester",
            "description": (
                "Runs a real HTTP load test against a URL and returns p50/p75/p95/p99 latency, "
                "error rates, throughput, and a latency histogram. Impossible to do accurately "
                "in a chat session."
            ),
            "endpoint_url": BUILTIN_INTERNAL_ENDPOINTS[LOAD_TESTER_AGENT_ID],
            "price_per_call_usd": 0.03,
            "tags": ["performance", "load-testing", "latency", "developer-tools"],
            "is_featured": True,
            "cacheable": False,
            "category": "Quality",
            "runtime_requirements": ["requests", "threading (stdlib)"],
            "tooling_kind": "live_network_checks",
            "stability_tier": "beta",
            "codex_recommended": True,
            "short_use_cases": [
                "measure API latency under load",
                "find p95 before launch",
                "detect performance regression",
            ],
            "match_keywords": [
                "load test", "load testing", "performance test", "latency", "throughput",
                "p95", "p99", "stress test", "benchmark endpoint",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Target URL to load test"},
                    "rps": {"type": "integer", "description": "Target requests per second (max 50)", "default": 5},
                    "duration_seconds": {"type": "integer", "description": "Test duration in seconds (max 30)", "default": 10},
                    "concurrency": {"type": "integer", "description": "Concurrent workers (max 20)", "default": 5},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"], "default": "GET"},
                    "headers": {"type": "object", "description": "Additional HTTP headers"},
                    "body": {"type": "string", "description": "Request body for POST/PUT"},
                    "expected_status": {"type": "integer", "default": 200},
                },
                "required": ["url"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "total_requests": {"type": "integer"},
                    "success_count": {"type": "integer"},
                    "error_count": {"type": "integer"},
                    "error_rate": {"type": "number"},
                    "throughput_rps": {"type": "number"},
                    "latency_ms": {
                        "type": "object",
                        "properties": {
                            "p50": {"type": "number"},
                            "p75": {"type": "number"},
                            "p95": {"type": "number"},
                            "p99": {"type": "number"},
                            "mean": {"type": "number"},
                            "min": {"type": "number"},
                            "max": {"type": "number"},
                        },
                    },
                    "status_codes": {"type": "object"},
                    "histogram": {"type": "array"},
                    "summary": {"type": "string"},
                },
                "required": ["total_requests", "success_count", "error_count", "latency_ms"],
            },
            "output_examples": [
                {
                    "input": {"url": "https://api.example.com/health", "rps": 10, "duration_seconds": 10},
                    "output": {
                        "url": "https://api.example.com/health",
                        "total_requests": 98,
                        "success_count": 98,
                        "error_count": 0,
                        "error_rate": 0.0,
                        "duration_actual_ms": 10050,
                        "throughput_rps": 9.75,
                        "latency_ms": {"p50": 28.4, "p75": 35.1, "p95": 62.3, "p99": 88.7, "mean": 31.2, "min": 18.1, "max": 95.4, "std_dev": 12.1},
                        "status_codes": {"200": 98},
                        "errors": [],
                        "histogram": [{"bucket_ms": 50, "count": 82}, {"bucket_ms": 100, "count": 16}],
                        "summary": "p50=28ms p95=62ms p99=89ms — 9.75 rps, 0% errors on 98 requests",
                    },
                }
            ],
        },
        # ------------------------------------------------------------------ #
        # CI Failure Reproducer                                                #
        # ------------------------------------------------------------------ #
        {
            "agent_id": CI_FAILURE_REPRODUCER_AGENT_ID,
            "name": "CI Failure Reproducer",
            "slug": "ci-failure-reproducer",
            "description": (
                "Reproduces CI failures by actually running the extracted or provided commands "
                "in a clean sandbox. Identifies failure type (code, dependency, env, config, flaky), "
                "returns diagnosis and suggested fix. Claude patches CI from log text; this runs the command."
            ),
            "endpoint_url": BUILTIN_INTERNAL_ENDPOINTS[CI_FAILURE_REPRODUCER_AGENT_ID],
            "price_per_call_usd": 0.05,
            "tags": ["ci", "debugging", "testing", "developer-tools"],
            "is_featured": True,
            "cacheable": False,
            "category": "Developer Tools",
            "runtime_requirements": ["python3", "subprocess (stdlib)"],
            "tooling_kind": "sandbox_execution",
            "stability_tier": "beta",
            "codex_recommended": True,
            "short_use_cases": [
                "reproduce pytest failure",
                "debug failing npm test",
                "identify flaky test",
            ],
            "match_keywords": [
                "ci failure", "ci error", "failing test", "pytest failure",
                "npm test failed", "github actions", "circleci", "reproduce failure",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "log": {
                        "type": "string",
                        "description": "CI failure log output (stdout + stderr from CI run)",
                    },
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Commands to run (extracted from log if not provided)",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "node", "go", "auto"],
                        "default": "auto",
                    },
                    "working_dir_files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                        "description": "Files needed to reproduce (requirements.txt, package.json, test files)",
                    },
                    "timeout_seconds": {"type": "integer", "default": 30, "maximum": 120},
                },
                "required": ["log"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "failure_type": {"type": "string", "enum": ["code_error", "dependency_error", "env_error", "config_error", "flaky_test", "timeout", "unknown"]},
                    "failing_command": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "diagnosis": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                    "reproduction_command": {"type": "string"},
                    "commands_tried": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["failure_type", "failing_command", "commands_tried"],
            },
            "output_examples": [
                {
                    "input": {
                        "log": "FAILED tests/test_api.py::test_auth - ModuleNotFoundError: No module named 'jwt'\nERROR: tests/test_api.py",
                        "working_dir_files": [{"name": "requirements.txt", "content": "fastapi\npydantic"}],
                    },
                    "output": {
                        "failure_type": "dependency_error",
                        "failing_command": "pytest tests/test_api.py",
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "ModuleNotFoundError: No module named 'jwt'",
                        "diagnosis": "The test imports 'jwt' but it's not in requirements.txt. Add 'PyJWT' to fix.",
                        "suggested_fix": "pip install PyJWT and add it to requirements.txt",
                        "reproduction_command": "pip install -r requirements.txt && pytest tests/test_api.py",
                        "commands_tried": [{"command": "pytest tests/test_api.py", "exit_code": 1, "duration_ms": 820}],
                    },
                }
            ],
        },
        # ------------------------------------------------------------------ #
        # Dockerfile Analyzer — security and best-practice linting            #
        # ------------------------------------------------------------------ #
        {
            "agent_id": DOCKERFILE_ANALYZER_AGENT_ID,
            "name": "Dockerfile Analyzer",
            "slug": "dockerfile-analyzer",
            "description": (
                "Runs hadolint and custom security checks on Dockerfiles. "
                "Finds unpinned images, secrets in ENV, root-user risks, and "
                "pipe-to-shell patterns. Returns scored findings."
            ),
            "endpoint_url": BUILTIN_INTERNAL_ENDPOINTS[DOCKERFILE_ANALYZER_AGENT_ID],
            "price_per_call_usd": 0.005,
            "tags": ["security", "docker", "containers", "devops", "static-analysis"],
            "is_featured": True,
            "cacheable": True,
            "category": "Security",
            "runtime_requirements": ["hadolint (optional — degrades to regex checks)"],
            "tooling_kind": "tool_execution",
            "stability_tier": "stable",
            "codex_recommended": True,
            "short_use_cases": [
                "lint a Dockerfile",
                "find security issues in Docker image",
                "check for unpinned base image",
            ],
            "match_keywords": [
                "dockerfile", "docker", "container", "hadolint",
                "base image", "docker security", "FROM latest",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "dockerfile": {
                        "type": "string",
                        "description": "The full contents of the Dockerfile to analyze.",
                    },
                },
                "required": ["dockerfile"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "findings": {"type": "array", "items": {"type": "object"}},
                    "total_findings": {"type": "integer"},
                    "by_severity": {"type": "object"},
                    "score": {"type": "integer"},
                    "pinned_base_image": {"type": "boolean"},
                    "runs_as_root": {"type": "boolean"},
                    "has_secrets_in_env": {"type": "boolean"},
                    "tool_used": {"type": "string"},
                },
                "required": ["findings", "total_findings", "by_severity", "score"],
            },
            "output_examples": [
                {
                    "input": {"dockerfile": "FROM ubuntu:latest\nRUN apt-get install curl\n"},
                    "output": {
                        "findings": [{"line": 1, "severity": "warning", "rule": "DL3007", "message": "Using latest is best avoided", "fix_hint": "Pin to a specific image tag"}],
                        "total_findings": 1,
                        "by_severity": {"error": 0, "warning": 1, "info": 0},
                        "score": 95,
                        "pinned_base_image": False,
                        "runs_as_root": True,
                        "has_secrets_in_env": False,
                        "tool_used": "regex",
                    },
                }
            ],
        },
        # ------------------------------------------------------------------ #
        # OpenAPI Validator — structural validation + breaking change detect  #
        # ------------------------------------------------------------------ #
        {
            "agent_id": OPENAPI_VALIDATOR_AGENT_ID,
            "name": "OpenAPI Validator",
            "slug": "openapi-validator",
            "description": (
                "Validates OpenAPI 3.x specs for structural correctness and detects "
                "breaking changes between two versions (removed endpoints, added required "
                "params, type changes)."
            ),
            "endpoint_url": BUILTIN_INTERNAL_ENDPOINTS[OPENAPI_VALIDATOR_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["api", "openapi", "validation", "breaking-changes", "developer-tools"],
            "is_featured": True,
            "cacheable": True,
            "category": "Developer Tools",
            "runtime_requirements": ["openapi-spec-validator (optional)", "PyYAML"],
            "tooling_kind": "tool_execution",
            "stability_tier": "stable",
            "codex_recommended": True,
            "short_use_cases": [
                "validate an OpenAPI spec",
                "find breaking API changes",
                "check spec structure",
            ],
            "match_keywords": [
                "openapi", "swagger", "api spec", "breaking change",
                "api validation", "yaml spec", "rest api",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "string",
                        "description": "OpenAPI 3.x spec in YAML or JSON string format.",
                    },
                    "previous_spec": {
                        "type": "string",
                        "description": "Optional previous version spec for breaking-change comparison.",
                    },
                },
                "required": ["spec"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "valid": {"type": "boolean"},
                    "errors": {"type": "array", "items": {"type": "string"}},
                    "warnings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "message": {"type": "string"},
                            },
                            "required": ["message"],
                        },
                    },
                    "breaking_changes": {"type": "array", "items": {"type": "object"}},
                    "stats": {"type": "object"},
                    "spec_title": {"type": "string"},
                    "spec_version": {"type": "string"},
                },
                "required": ["valid", "errors", "stats"],
            },
            "output_examples": [
                {
                    "input": {"spec": "openapi: 3.0.3\ninfo:\n  title: My API\n  version: 1.0.0\npaths: {}"},
                    "output": {
                        "valid": True,
                        "errors": [],
                        "warnings": [],
                        "breaking_changes": [],
                        "stats": {"endpoints": 8, "schemas": 4, "parameters": 12, "openapi_version": "3.0.3"},
                        "spec_title": "My API",
                        "spec_version": "1.0.0",
                    },
                }
            ],
        },
        # ------------------------------------------------------------------ #
        # Coverage Runner — pytest coverage in isolated sandbox               #
        # ------------------------------------------------------------------ #
        {
            "agent_id": COVERAGE_RUNNER_AGENT_ID,
            "name": "Coverage Runner",
            "slug": "coverage-runner",
            "description": (
                "Runs pytest with coverage in an isolated sandbox. Returns overall "
                "coverage percentage, per-file uncovered line numbers, and branch "
                "coverage data."
            ),
            "endpoint_url": BUILTIN_INTERNAL_ENDPOINTS[COVERAGE_RUNNER_AGENT_ID],
            "price_per_call_usd": 0.02,
            "tags": ["testing", "coverage", "pytest", "code-quality", "sandbox"],
            "is_featured": True,
            "cacheable": False,
            "category": "Code Execution",
            "runtime_requirements": ["python3", "pytest", "coverage"],
            "tooling_kind": "sandbox_execution",
            "stability_tier": "stable",
            "codex_recommended": True,
            "short_use_cases": [
                "measure test coverage",
                "find uncovered lines",
                "check coverage threshold",
            ],
            "match_keywords": [
                "coverage", "test coverage", "pytest", "uncovered lines",
                "branch coverage", "code coverage", "missing coverage",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of {name, content} file objects to write into the sandbox.",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Optional minimum coverage percentage (0–100) to enforce.",
                    },
                    "test_path": {
                        "type": "string",
                        "description": "Optional path or pattern to pass to pytest (default: auto-discover).",
                    },
                },
                "required": ["files"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "overall_pct": {"type": "number"},
                    "passed_threshold": {"type": "boolean"},
                    "files": {"type": "array", "items": {"type": "object"}},
                    "total_statements": {"type": "integer"},
                    "total_missing": {"type": "integer"},
                    "exit_code": {"type": "integer"},
                },
                "required": ["overall_pct", "exit_code", "files"],
            },
            "output_examples": [
                {
                    "input": {
                        "files": [
                            {"name": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
                            {"name": "test_calculator.py", "content": "from calculator import add\ndef test_add():\n    assert add(1, 2) == 3\n"},
                        ],
                        "threshold": 80,
                    },
                    "output": {
                        "overall_pct": 78.5,
                        "passed_threshold": True,
                        "files": [{"name": "calculator.py", "coverage_pct": 85.0, "total_statements": 20, "missing_count": 3, "uncovered_lines": [14, 15, 22]}],
                        "total_statements": 40,
                        "total_missing": 8,
                        "exit_code": 0,
                    },
                }
            ],
        },
    ]
