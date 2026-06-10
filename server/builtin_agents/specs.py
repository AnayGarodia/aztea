"""Compose built-in agent registration specs from split modules."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from server.builtin_agents.constants import (
    ACCESSIBILITY_AUDITOR_AGENT_ID,
    ARCHIVE_INSPECTOR_AGENT_ID,
    BROKEN_LINK_CRAWLER_AGENT_ID,
    BROWSER_AGENT_ID,
    CI_FAILURE_REPRODUCER_AGENT_ID,
    COVERAGE_RUNNER_AGENT_ID,
    CURATED_BUILTIN_AGENT_IDS,
    CVELOOKUP_AGENT_ID,
    DB_SANDBOX_AGENT_ID,
    DEPENDENCY_AUDITOR_AGENT_ID,
    DIFF_ANALYZER_AGENT_ID,
    DNS_INSPECTOR_AGENT_ID,
    DOCKERFILE_ANALYZER_AGENT_ID,
    DOCS_GROUNDER_AGENT_ID,
    GITHUB_RELEASES_AGENT_ID,
    HCL_TERRAFORM_ANALYZER_AGENT_ID,
    HN_DIGEST_AGENT_ID,
    JWT_VALIDATOR_AGENT_ID,
    K8S_MANIFEST_VALIDATOR_AGENT_ID,
    LIGHTHOUSE_AUDITOR_AGENT_ID,
    LIVE_SANDBOX_AGENT_ID,
    LOAD_TESTER_AGENT_ID,
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID,
    OPENAPI_VALIDATOR_AGENT_ID,
    PDF_DOCUMENT_PARSER_AGENT_ID,
    PYPI_METADATA_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID,
    QUALITY_JUDGE_AGENT_ID,
    REGEX_TESTER_AGENT_ID,
    SAST_SCANNER_AGENT_ID,
    SBOM_GENERATOR_AGENT_ID,
    SECRET_SCANNER_AGENT_ID,
    SECURITY_HEADERS_GRADER_AGENT_ID,
    SSL_CERTIFICATE_DECODER_AGENT_ID,
    STRIPE_WEBHOOK_DEBUGGER_AGENT_ID,
    TERRAFORM_PLAN_ANALYZER_AGENT_ID,
    VIDEO_STORYBOARD_AGENT_ID,
    UNICODE_INSPECTOR_AGENT_ID,
    VISUAL_REGRESSION_AGENT_ID,
    WEB_SEARCH_AGENT_ID,
)
from server.builtin_agents.specs_part1 import load_builtin_specs_part1
from server.builtin_agents.specs_part2 import load_builtin_specs_part2
from server.builtin_agents.specs_part3 import load_builtin_specs_part3
from server.builtin_agents.specs_part4 import load_builtin_specs_part4
from server.builtin_agents.specs_part5 import load_builtin_specs_part5
from server.builtin_agents.specs_part6 import load_builtin_specs_part6
from server.builtin_agents.specs_part7 import load_builtin_specs_part7
from server.builtin_agents.specs_part8 import load_builtin_specs_part8
from server.builtin_agents.specs_part9 import load_builtin_specs_part9
from server.builtin_agents.specs_part10 import load_builtin_specs_part10
from server.builtin_agents.specs_part11 import load_builtin_specs_part11
from server.builtin_agents.specs_part12 import load_builtin_specs_part12

_DEFAULT_CATEGORY_BY_AGENT_ID = {
    QUALITY_JUDGE_AGENT_ID: "Internal",
    CVELOOKUP_AGENT_ID: "Security",
    VIDEO_STORYBOARD_AGENT_ID: "Media",
    PYTHON_EXECUTOR_AGENT_ID: "Code Execution",
    HN_DIGEST_AGENT_ID: "Research",
    DNS_INSPECTOR_AGENT_ID: "Security",
    DEPENDENCY_AUDITOR_AGENT_ID: "Code",
    BROWSER_AGENT_ID: "Web",
    SECRET_SCANNER_AGENT_ID: "Security",
    LIGHTHOUSE_AUDITOR_AGENT_ID: "Quality",
    ACCESSIBILITY_AUDITOR_AGENT_ID: "Quality",
    SECURITY_HEADERS_GRADER_AGENT_ID: "Security",
    BROKEN_LINK_CRAWLER_AGENT_ID: "Quality",
    PDF_DOCUMENT_PARSER_AGENT_ID: "Research",
    WEB_SEARCH_AGENT_ID: "Research",
    DOCS_GROUNDER_AGENT_ID: "Research",
    SAST_SCANNER_AGENT_ID: "Security",
    STRIPE_WEBHOOK_DEBUGGER_AGENT_ID: "Developer Tools",
    LOAD_TESTER_AGENT_ID: "QA",
    CI_FAILURE_REPRODUCER_AGENT_ID: "Developer Tools",
    DOCKERFILE_ANALYZER_AGENT_ID: "Security",
    OPENAPI_VALIDATOR_AGENT_ID: "Developer Tools",
    COVERAGE_RUNNER_AGENT_ID: "Code Execution",
    SSL_CERTIFICATE_DECODER_AGENT_ID: "Security",
    DIFF_ANALYZER_AGENT_ID: "Code",
    K8S_MANIFEST_VALIDATOR_AGENT_ID: "Developer Tools",
    ARCHIVE_INSPECTOR_AGENT_ID: "Security",
    UNICODE_INSPECTOR_AGENT_ID: "Security",
    TERRAFORM_PLAN_ANALYZER_AGENT_ID: "Developer Tools",
    LIVE_SANDBOX_AGENT_ID: "Developer Tools",
    REGEX_TESTER_AGENT_ID: "Developer Tools",
    JWT_VALIDATOR_AGENT_ID: "Security",
    SBOM_GENERATOR_AGENT_ID: "Security",
    PYPI_METADATA_AGENT_ID: "Developer Tools",
    GITHUB_RELEASES_AGENT_ID: "Developer Tools",
    HCL_TERRAFORM_ANALYZER_AGENT_ID: "Security",
}

_DEFAULT_CACHEABLE_BY_AGENT_ID = {
    QUALITY_JUDGE_AGENT_ID: False,
    CVELOOKUP_AGENT_ID: True,
    VIDEO_STORYBOARD_AGENT_ID: False,
    # 1.7.4 — flipped True. The python sandbox captures stdout/stderr
    # deterministically; replaying a cache hit returns the same captured
    # output a fresh run would produce. Pre-1.7.4 ten identical
    # concurrent calls produced ten distinct charges (1.7.1 eval N15);
    # this default + the same-eval removal of the agent from
    # core/cache.py's _NON_CACHEABLE_INTERNAL_ENDPOINTS together make
    # the cache actually hit. Users wanting 10 distinct runs (e.g.
    # `print(time.time())`) should use distinct inputs.
    PYTHON_EXECUTOR_AGENT_ID: True,
    HN_DIGEST_AGENT_ID: True,
    DNS_INSPECTOR_AGENT_ID: False,
    DEPENDENCY_AUDITOR_AGENT_ID: True,
    SECRET_SCANNER_AGENT_ID: True,
    LIGHTHOUSE_AUDITOR_AGENT_ID: False,
    ACCESSIBILITY_AUDITOR_AGENT_ID: False,
    SECURITY_HEADERS_GRADER_AGENT_ID: False,
    BROKEN_LINK_CRAWLER_AGENT_ID: False,
    PDF_DOCUMENT_PARSER_AGENT_ID: True,
    WEB_SEARCH_AGENT_ID: False,
    DOCS_GROUNDER_AGENT_ID: True,
    SAST_SCANNER_AGENT_ID: True,
    STRIPE_WEBHOOK_DEBUGGER_AGENT_ID: False,
    LOAD_TESTER_AGENT_ID: False,
    CI_FAILURE_REPRODUCER_AGENT_ID: False,
    DOCKERFILE_ANALYZER_AGENT_ID: True,
    OPENAPI_VALIDATOR_AGENT_ID: True,
    COVERAGE_RUNNER_AGENT_ID: False,
    SSL_CERTIFICATE_DECODER_AGENT_ID: True,
    DIFF_ANALYZER_AGENT_ID: True,
    K8S_MANIFEST_VALIDATOR_AGENT_ID: False,
    ARCHIVE_INSPECTOR_AGENT_ID: True,
    UNICODE_INSPECTOR_AGENT_ID: True,
    TERRAFORM_PLAN_ANALYZER_AGENT_ID: True,
    LIVE_SANDBOX_AGENT_ID: False,
    REGEX_TESTER_AGENT_ID: True,
    JWT_VALIDATOR_AGENT_ID: False,  # tokens are sensitive — never cache.
    SBOM_GENERATOR_AGENT_ID: True,
    PYPI_METADATA_AGENT_ID: True,
    GITHUB_RELEASES_AGENT_ID: False,  # releases change with new tags.
    HCL_TERRAFORM_ANALYZER_AGENT_ID: True,
}

_DEFAULT_RUNTIME_REQUIREMENTS_BY_AGENT_ID = {
    CVELOOKUP_AGENT_ID: ["requests"],
    VIDEO_STORYBOARD_AGENT_ID: ["configured media backend"],
    PYTHON_EXECUTOR_AGENT_ID: ["python3"],
    HN_DIGEST_AGENT_ID: ["httpx", "llm provider optional for synthesis"],
    DNS_INSPECTOR_AGENT_ID: ["socket", "ssl"],
    DEPENDENCY_AUDITOR_AGENT_ID: ["requests"],
    BROWSER_AGENT_ID: ["playwright", "chromium"],
    SECRET_SCANNER_AGENT_ID: [],
    LIGHTHOUSE_AUDITOR_AGENT_ID: ["lighthouse cli", "node>=18", "chromium"],
    ACCESSIBILITY_AUDITOR_AGENT_ID: ["playwright", "chromium"],
    SECURITY_HEADERS_GRADER_AGENT_ID: ["httpx"],
    BROKEN_LINK_CRAWLER_AGENT_ID: ["httpx", "beautifulsoup4"],
    PDF_DOCUMENT_PARSER_AGENT_ID: ["pymupdf", "pdfplumber"],
    WEB_SEARCH_AGENT_ID: ["BRAVE_SEARCH_API_KEY"],
    DOCS_GROUNDER_AGENT_ID: ["httpx", "llm provider optional for synthesis"],
    SAST_SCANNER_AGENT_ID: ["semgrep (optional)", "bandit (optional for Python)"],
    STRIPE_WEBHOOK_DEBUGGER_AGENT_ID: ["requests"],
    LOAD_TESTER_AGENT_ID: ["requests"],
    CI_FAILURE_REPRODUCER_AGENT_ID: ["python3", "node optional"],
    DOCKERFILE_ANALYZER_AGENT_ID: ["hadolint (optional)"],
    OPENAPI_VALIDATOR_AGENT_ID: ["openapi-spec-validator (optional)", "PyYAML"],
    COVERAGE_RUNNER_AGENT_ID: ["python3", "pytest", "coverage"],
    SSL_CERTIFICATE_DECODER_AGENT_ID: ["cryptography"],
    DIFF_ANALYZER_AGENT_ID: [],
    K8S_MANIFEST_VALIDATOR_AGENT_ID: ["kubectl (optional)", "PyYAML"],
    ARCHIVE_INSPECTOR_AGENT_ID: [],
    UNICODE_INSPECTOR_AGENT_ID: [],
    TERRAFORM_PLAN_ANALYZER_AGENT_ID: [],
    LIVE_SANDBOX_AGENT_ID: [
        "docker (daemon reachable from server)",
        "git",
        "libfaketime (optional)",
        "rsync (optional)",
    ],
    REGEX_TESTER_AGENT_ID: [],
    JWT_VALIDATOR_AGENT_ID: ["PyJWT (optional, for signature verification)"],
    SBOM_GENERATOR_AGENT_ID: [],
    PYPI_METADATA_AGENT_ID: ["requests"],
    GITHUB_RELEASES_AGENT_ID: ["requests"],
    HCL_TERRAFORM_ANALYZER_AGENT_ID: ["checkov (pip install)"],
}

_DEFAULT_TOOLING_KIND_BY_AGENT_ID = {
    QUALITY_JUDGE_AGENT_ID: "internal_judge",
    CVELOOKUP_AGENT_ID: "live_api",
    VIDEO_STORYBOARD_AGENT_ID: "model_backend",
    PYTHON_EXECUTOR_AGENT_ID: "sandbox_execution",
    HN_DIGEST_AGENT_ID: "live_fetch_plus_llm",
    DNS_INSPECTOR_AGENT_ID: "live_network_checks",
    DEPENDENCY_AUDITOR_AGENT_ID: "live_api_analysis",
    BROWSER_AGENT_ID: "browser_automation",
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID: "sandbox_execution",
    SECRET_SCANNER_AGENT_ID: "tool_execution",
    LIGHTHOUSE_AUDITOR_AGENT_ID: "browser_automation",
    ACCESSIBILITY_AUDITOR_AGENT_ID: "browser_automation",
    SECURITY_HEADERS_GRADER_AGENT_ID: "live_network_checks",
    BROKEN_LINK_CRAWLER_AGENT_ID: "live_network_checks",
    PDF_DOCUMENT_PARSER_AGENT_ID: "live_fetch_plus_parse",
    WEB_SEARCH_AGENT_ID: "live_api",
    DOCS_GROUNDER_AGENT_ID: "live_fetch_plus_llm",
    SAST_SCANNER_AGENT_ID: "tool_execution",
    STRIPE_WEBHOOK_DEBUGGER_AGENT_ID: "live_network_checks",
    LOAD_TESTER_AGENT_ID: "live_network_checks",
    CI_FAILURE_REPRODUCER_AGENT_ID: "sandbox_execution",
    DOCKERFILE_ANALYZER_AGENT_ID: "tool_execution",
    OPENAPI_VALIDATOR_AGENT_ID: "tool_execution",
    COVERAGE_RUNNER_AGENT_ID: "sandbox_execution",
    SSL_CERTIFICATE_DECODER_AGENT_ID: "tool_execution",
    DIFF_ANALYZER_AGENT_ID: "tool_execution",
    K8S_MANIFEST_VALIDATOR_AGENT_ID: "tool_execution",
    ARCHIVE_INSPECTOR_AGENT_ID: "tool_execution",
    UNICODE_INSPECTOR_AGENT_ID: "tool_execution",
    TERRAFORM_PLAN_ANALYZER_AGENT_ID: "tool_execution",
    LIVE_SANDBOX_AGENT_ID: "sandbox_orchestration",
    REGEX_TESTER_AGENT_ID: "tool_execution",
    JWT_VALIDATOR_AGENT_ID: "tool_execution",
    SBOM_GENERATOR_AGENT_ID: "tool_execution",
    PYPI_METADATA_AGENT_ID: "live_api",
    GITHUB_RELEASES_AGENT_ID: "live_api",
    HCL_TERRAFORM_ANALYZER_AGENT_ID: "tool_execution",
}

_DEFAULT_STABILITY_TIER_BY_AGENT_ID = {
    QUALITY_JUDGE_AGENT_ID: "internal",
    CVELOOKUP_AGENT_ID: "stable",
    VIDEO_STORYBOARD_AGENT_ID: "experimental",
    PYTHON_EXECUTOR_AGENT_ID: "stable",
    HN_DIGEST_AGENT_ID: "stable",
    DNS_INSPECTOR_AGENT_ID: "stable",
    DEPENDENCY_AUDITOR_AGENT_ID: "stable",
    BROWSER_AGENT_ID: "beta",
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID: "experimental",
    SECRET_SCANNER_AGENT_ID: "stable",
    LIGHTHOUSE_AUDITOR_AGENT_ID: "beta",
    ACCESSIBILITY_AUDITOR_AGENT_ID: "beta",
    SECURITY_HEADERS_GRADER_AGENT_ID: "stable",
    BROKEN_LINK_CRAWLER_AGENT_ID: "beta",
    PDF_DOCUMENT_PARSER_AGENT_ID: "beta",
    WEB_SEARCH_AGENT_ID: "beta",
    DOCS_GROUNDER_AGENT_ID: "beta",
    SAST_SCANNER_AGENT_ID: "beta",
    STRIPE_WEBHOOK_DEBUGGER_AGENT_ID: "beta",
    LOAD_TESTER_AGENT_ID: "beta",
    CI_FAILURE_REPRODUCER_AGENT_ID: "beta",
    DOCKERFILE_ANALYZER_AGENT_ID: "stable",
    OPENAPI_VALIDATOR_AGENT_ID: "stable",
    COVERAGE_RUNNER_AGENT_ID: "stable",
    SSL_CERTIFICATE_DECODER_AGENT_ID: "stable",
    DIFF_ANALYZER_AGENT_ID: "stable",
    K8S_MANIFEST_VALIDATOR_AGENT_ID: "beta",
    ARCHIVE_INSPECTOR_AGENT_ID: "stable",
    UNICODE_INSPECTOR_AGENT_ID: "stable",
    TERRAFORM_PLAN_ANALYZER_AGENT_ID: "stable",
    LIVE_SANDBOX_AGENT_ID: "beta",
    REGEX_TESTER_AGENT_ID: "stable",
    JWT_VALIDATOR_AGENT_ID: "beta",
    SBOM_GENERATOR_AGENT_ID: "beta",
    PYPI_METADATA_AGENT_ID: "stable",
    GITHUB_RELEASES_AGENT_ID: "stable",
    HCL_TERRAFORM_ANALYZER_AGENT_ID: "beta",
}

_DEFAULT_CODEX_RECOMMENDED_BY_AGENT_ID = {
    QUALITY_JUDGE_AGENT_ID: False,
    CVELOOKUP_AGENT_ID: True,
    VIDEO_STORYBOARD_AGENT_ID: False,
    PYTHON_EXECUTOR_AGENT_ID: True,
    HN_DIGEST_AGENT_ID: False,
    DNS_INSPECTOR_AGENT_ID: True,
    DEPENDENCY_AUDITOR_AGENT_ID: True,
    BROWSER_AGENT_ID: False,
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID: False,
    SECRET_SCANNER_AGENT_ID: True,
    LIGHTHOUSE_AUDITOR_AGENT_ID: True,
    ACCESSIBILITY_AUDITOR_AGENT_ID: True,
    SECURITY_HEADERS_GRADER_AGENT_ID: True,
    BROKEN_LINK_CRAWLER_AGENT_ID: True,
    PDF_DOCUMENT_PARSER_AGENT_ID: False,
    WEB_SEARCH_AGENT_ID: True,
    DOCS_GROUNDER_AGENT_ID: True,
    SAST_SCANNER_AGENT_ID: True,
    STRIPE_WEBHOOK_DEBUGGER_AGENT_ID: True,
    LOAD_TESTER_AGENT_ID: True,
    CI_FAILURE_REPRODUCER_AGENT_ID: True,
    DOCKERFILE_ANALYZER_AGENT_ID: True,
    OPENAPI_VALIDATOR_AGENT_ID: True,
    COVERAGE_RUNNER_AGENT_ID: True,
    SSL_CERTIFICATE_DECODER_AGENT_ID: True,
    DIFF_ANALYZER_AGENT_ID: True,
    K8S_MANIFEST_VALIDATOR_AGENT_ID: True,
    ARCHIVE_INSPECTOR_AGENT_ID: True,
    UNICODE_INSPECTOR_AGENT_ID: True,
    TERRAFORM_PLAN_ANALYZER_AGENT_ID: True,
    LIVE_SANDBOX_AGENT_ID: True,
    REGEX_TESTER_AGENT_ID: True,
    JWT_VALIDATOR_AGENT_ID: True,
    SBOM_GENERATOR_AGENT_ID: True,
    PYPI_METADATA_AGENT_ID: True,
    GITHUB_RELEASES_AGENT_ID: True,
    HCL_TERRAFORM_ANALYZER_AGENT_ID: True,
}

_DEFAULT_SHORT_USE_CASES_BY_AGENT_ID = {
    CVELOOKUP_AGENT_ID: ["look up a CVE ID", "check affected package versions"],
    PYTHON_EXECUTOR_AGENT_ID: ["run a snippet", "verify runtime behavior"],
    DNS_INSPECTOR_AGENT_ID: ["check DNS", "check SSL expiry", "inspect headers"],
    DEPENDENCY_AUDITOR_AGENT_ID: ["audit requirements.txt", "audit package.json"],
    DB_SANDBOX_AGENT_ID: ["test SQL", "inspect query plans"],
    VISUAL_REGRESSION_AGENT_ID: ["compare screenshots", "highlight changed pixels"],
    BROWSER_AGENT_ID: ["render a page", "capture screenshot of SPA"],
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID: ["run JS/TS", "run Go", "run Rust"],
    SECRET_SCANNER_AGENT_ID: [
        "scan a file for leaked keys",
        "audit .env for secrets",
        "pre-commit credential check",
    ],
    LIGHTHOUSE_AUDITOR_AGENT_ID: [
        "audit a launch site",
        "score Web Vitals",
        "find perf regressions",
    ],
    ACCESSIBILITY_AUDITOR_AGENT_ID: [
        "WCAG audit a page",
        "find a11y violations",
        "spot missing alt text",
    ],
    SECURITY_HEADERS_GRADER_AGENT_ID: [
        "grade security headers",
        "check CSP / HSTS",
        "pre-launch security review",
    ],
    BROKEN_LINK_CRAWLER_AGENT_ID: [
        "find 404s",
        "audit redirect chains",
        "spot mixed content",
    ],
    PDF_DOCUMENT_PARSER_AGENT_ID: [
        "extract a PDF whitepaper",
        "pull tables from a research paper",
        "summarize a long PDF",
    ],
    WEB_SEARCH_AGENT_ID: [
        "search the live web",
        "find recent news",
        "compare top results",
    ],
    DOCS_GROUNDER_AGENT_ID: [
        "look up current Stripe API",
        "find Next.js migration notes",
        "get exact function signatures",
    ],
    SAST_SCANNER_AGENT_ID: [
        "scan for SQL injection",
        "find hardcoded secrets",
        "run semgrep on a PR diff",
    ],
    STRIPE_WEBHOOK_DEBUGGER_AGENT_ID: [
        "test webhook signature verification",
        "debug Stripe idempotency",
        "replay a checkout.session.completed event",
    ],
    LOAD_TESTER_AGENT_ID: [
        "measure p99 latency",
        "load-test an API endpoint",
        "stress-test before launch",
    ],
    CI_FAILURE_REPRODUCER_AGENT_ID: [
        "reproduce a flaky test",
        "diagnose a failing CI step",
        "isolate a dependency error",
    ],
    DOCKERFILE_ANALYZER_AGENT_ID: ["lint a Dockerfile", "find Docker security issues", "check for unpinned base image"],
    OPENAPI_VALIDATOR_AGENT_ID: ["validate an OpenAPI spec", "find breaking API changes", "check spec structure"],
    COVERAGE_RUNNER_AGENT_ID: ["measure test coverage", "find uncovered lines", "check coverage threshold"],
    SSL_CERTIFICATE_DECODER_AGENT_ID: ["decode a PEM certificate", "extract SANs and expiry", "check certificate chain order"],
    DIFF_ANALYZER_AGENT_ID: ["analyze PR risk", "detect migration in diff", "find secret additions in diff"],
    K8S_MANIFEST_VALIDATOR_AGENT_ID: ["validate k8s YAML", "find unpinned images", "check resource limits"],
    ARCHIVE_INSPECTOR_AGENT_ID: ["inspect zip contents", "detect zip bomb", "find path traversal in archive"],
    UNICODE_INSPECTOR_AGENT_ID: ["detect homoglyphs", "find invisible chars", "check bidi attack"],
    TERRAFORM_PLAN_ANALYZER_AGENT_ID: ["analyze terraform plan", "find risky destroys", "classify IaC changes"],
    LIVE_SANDBOX_AGENT_ID: [
        "spin up the user's repo and run the test suite",
        "reproduce a bug in a clone of production",
        "snapshot then test a risky migration",
        "fork a sandbox to try multiple fixes in parallel",
    ],
    REGEX_TESTER_AGENT_ID: [
        "test a regex against sample strings",
        "verify capture groups",
        "check regex flag behavior",
    ],
    JWT_VALIDATOR_AGENT_ID: [
        "decode a JWT and inspect claims",
        "verify a JWT signature against a JWKS",
        "check exp/nbf claims",
    ],
    SBOM_GENERATOR_AGENT_ID: [
        "generate a CycloneDX SBOM from requirements.txt",
        "list direct dependencies with Package URLs",
        "build an SBOM from a package.json",
    ],
    PYPI_METADATA_AGENT_ID: [
        "look up latest_version + license for a Python package",
        "check requires_python for compatibility",
        "find release date for a specific version",
    ],
    GITHUB_RELEASES_AGENT_ID: [
        "list recent releases for a repo",
        "find releases newer than a tag",
        "fetch a release body / changelog",
    ],
    HCL_TERRAFORM_ANALYZER_AGENT_ID: [
        "scan raw Terraform HCL for misconfigurations",
        "find S3 buckets without encryption / logging",
        "run a CIS-only checkov pass",
    ],
}


def _validate_jsonschema_shape(schema: Any, *, field: str, agent_id: str) -> None:
    """Catch malformed input/output schemas at module load instead of letting
    a typo silently break the MCP manifest. We only enforce the shape we
    actually depend on: a dict with type='object' and a dict 'properties'
    if present. Stricter JSON Schema validation lives in the runtime
    request validators, not here."""
    if not isinstance(schema, dict):
        raise ValueError(
            f"Built-in spec {agent_id}: {field} must be a dict, got {type(schema).__name__}."
        )
    declared_type = schema.get("type")
    if declared_type is not None and declared_type != "object":
        raise ValueError(
            f"Built-in spec {agent_id}: {field}.type must be 'object', got {declared_type!r}."
        )
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise ValueError(
            f"Built-in spec {agent_id}: {field}.properties must be a dict, got {type(properties).__name__}."
        )
    required = schema.get("required")
    if required is not None and not (
        isinstance(required, list) and all(isinstance(r, str) for r in required)
    ):
        raise ValueError(
            f"Built-in spec {agent_id}: {field}.required must be a list of strings."
        )


def _normalize_builtin_spec(spec: dict[str, Any]) -> dict[str, Any]:
    agent_id = str(spec.get("agent_id") or "").strip()
    if not agent_id:
        raise ValueError("Built-in spec is missing agent_id.")
    endpoint_url = str(spec.get("endpoint_url") or "").strip()
    if not endpoint_url.startswith("internal://"):
        raise ValueError(f"Built-in spec {agent_id} must use an internal:// endpoint.")
    output_examples = spec.get("output_examples")
    if not isinstance(output_examples, list) or not output_examples:
        raise ValueError(
            f"Built-in spec {agent_id} must include at least one output example."
        )
    _validate_jsonschema_shape(spec.get("input_schema"), field="input_schema", agent_id=agent_id)
    _validate_jsonschema_shape(spec.get("output_schema"), field="output_schema", agent_id=agent_id)
    if agent_id in CURATED_BUILTIN_AGENT_IDS:
        category = str(
            spec.get("category") or _DEFAULT_CATEGORY_BY_AGENT_ID.get(agent_id) or ""
        ).strip()
        if not category:
            raise ValueError(f"Built-in spec {agent_id} is missing category metadata.")
        cacheable = spec.get("cacheable")
        if cacheable is None:
            if agent_id not in _DEFAULT_CACHEABLE_BY_AGENT_ID:
                raise ValueError(
                    f"Built-in spec {agent_id} is missing cacheable metadata."
                )
            cacheable = _DEFAULT_CACHEABLE_BY_AGENT_ID[agent_id]
        runtime_requirements = spec.get("runtime_requirements")
        if runtime_requirements is None:
            runtime_requirements = _DEFAULT_RUNTIME_REQUIREMENTS_BY_AGENT_ID.get(
                agent_id, []
            )
        return {
            **spec,
            "category": category,
            "cacheable": bool(cacheable),
            "is_featured": bool(spec.get("is_featured", True)),
            "runtime_requirements": list(runtime_requirements),
            "tooling_kind": str(
                spec.get("tooling_kind")
                or _DEFAULT_TOOLING_KIND_BY_AGENT_ID.get(agent_id)
                or "tool_execution"
            ),
            "stability_tier": str(
                spec.get("stability_tier")
                or _DEFAULT_STABILITY_TIER_BY_AGENT_ID.get(agent_id)
                or "stable"
            ),
            "codex_recommended": bool(
                spec.get(
                    "codex_recommended",
                    _DEFAULT_CODEX_RECOMMENDED_BY_AGENT_ID.get(agent_id, False),
                )
            ),
            "short_use_cases": list(
                spec.get("short_use_cases")
                or _DEFAULT_SHORT_USE_CASES_BY_AGENT_ID.get(agent_id, [])
            ),
        }
    return dict(spec)


@lru_cache(maxsize=1)
def _all_builtin_specs() -> tuple[dict[str, Any], ...]:
    specs = load_builtin_specs_part1()
    specs.extend(load_builtin_specs_part2())
    specs.extend(load_builtin_specs_part3())
    specs.extend(load_builtin_specs_part4())
    specs.extend(load_builtin_specs_part5())
    specs.extend(load_builtin_specs_part6())
    specs.extend(load_builtin_specs_part7())
    specs.extend(load_builtin_specs_part8())
    specs.extend(load_builtin_specs_part9())
    specs.extend(load_builtin_specs_part10())
    specs.extend(load_builtin_specs_part11())
    specs.extend(load_builtin_specs_part12())
    normalized = [_normalize_builtin_spec(spec) for spec in specs]
    seen_ids: set[str] = set()
    seen_endpoints: set[str] = set()
    for spec in normalized:
        agent_id = str(spec["agent_id"])
        endpoint_url = str(spec["endpoint_url"])
        if agent_id in seen_ids:
            raise ValueError(f"Duplicate built-in spec agent_id: {agent_id}")
        if endpoint_url in seen_endpoints:
            raise ValueError(f"Duplicate built-in spec endpoint_url: {endpoint_url}")
        seen_ids.add(agent_id)
        seen_endpoints.add(endpoint_url)
    return tuple(normalized)


def builtin_agent_specs() -> list[dict[str, Any]]:
    return [
        spec
        for spec in _all_builtin_specs()
        if spec.get("agent_id") in CURATED_BUILTIN_AGENT_IDS
    ]


@lru_cache(maxsize=1)
def builtin_spec_by_id() -> dict[str, dict[str, Any]]:
    return {str(spec["agent_id"]): dict(spec) for spec in builtin_agent_specs()}


def builtin_catalog_metadata(agent_id: str) -> dict[str, Any] | None:
    spec = builtin_spec_by_id().get(str(agent_id or "").strip())
    if spec is None:
        return None
    return {
        "category": spec.get("category"),
        "is_featured": bool(spec.get("is_featured", False)),
        "cacheable": spec.get("cacheable"),
        "runtime_requirements": list(spec.get("runtime_requirements") or []),
        "tooling_kind": spec.get("tooling_kind"),
        "stability_tier": spec.get("stability_tier"),
        "codex_recommended": bool(spec.get("codex_recommended", False)),
        "short_use_cases": list(spec.get("short_use_cases") or []),
        "deprecated": False,
    }


# Eager routing-overlay registration at module import time. Each uvicorn
# worker imports `server.application`, which (transitively) imports this
# module — so this runs once per process, deterministically, before any
# request can reach the search ranker. Replaces the previous lifespan-
# based hook which was racy: prod verification on a26b00e showed only 1
# of 3 workers populating the overlay through that path. The lazy
# self-heal in core.registry.agents_ops (e7a8073) is kept as a safety
# net for any future code path that imports core/registry without
# importing server first (e.g. unit tests, cron jobs).
def _install_routing_overlay() -> None:
    try:
        from core.registry.agents_ops import set_routing_overlay

        specs = builtin_agent_specs()
        set_routing_overlay(
            match_keywords={
                str(spec.get("agent_id") or ""): list(spec.get("match_keywords") or [])
                for spec in specs
                if spec.get("match_keywords")
            },
            block_keywords={
                str(spec.get("agent_id") or ""): list(spec.get("block_keywords") or [])
                for spec in specs
                if spec.get("block_keywords")
            },
        )
    except Exception:  # noqa: BLE001 — search must never crash on overlay load
        # If the eager registration fails (import order, partial init),
        # the lazy self-heal in core.registry.agents_ops takes over on
        # first read. We intentionally swallow the exception here so
        # importing this module never aborts startup.
        pass


_install_routing_overlay()
