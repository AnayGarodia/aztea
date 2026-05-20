"""Deterministic IDs, endpoint maps, and curated visibility for built-in agents."""

from __future__ import annotations

import os

SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "http://localhost:8000").rstrip("/")


def normalize_endpoint_ref(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


CODEREVIEW_AGENT_ID = "8cea848f-a165-5d6c-b1a0-7d14fff77d14"
CVELOOKUP_AGENT_ID = "a3e239dd-ea92-556b-9c95-0a213a3daf59"
# WHY: QUALITY_JUDGE_AGENT_ID is the internal-only agent that handles
# quality-judge sweeps and dispute LLM judging. It is intentionally NOT
# in CURATED_PUBLIC_BUILTIN_AGENT_IDS — it has no caller-facing input
# schema, no price (compensated from the judges-pool), and cannot be
# directly hired. Every job row carries this id in the `judge_agent_id`
# column so dispute resolution can route to it; that surfaces the id
# back to callers as "hidden agent" — by design, not an oversight.
# Bug 14 (2026-05-18 audit): documented here so callers who inspect a
# job record know what this id refers to.
QUALITY_JUDGE_AGENT_ID = "9cf0d9d0-4a10-58c9-b97a-6b5f81b1cf33"
VIDEO_STORYBOARD_AGENT_ID = "c12994de-cde9-514a-9c07-a3833b25bb1f"
PYTHON_EXECUTOR_AGENT_ID = "040dc3f5-afe7-5db7-b253-4936090cc7af"
HN_DIGEST_AGENT_ID = "31cc3a99-eca6-5202-96d4-8366f426ae1d"
DNS_INSPECTOR_AGENT_ID = "3d677381-791c-5e83-8e66-5b77d0e43e2e"
DEPENDENCY_AUDITOR_AGENT_ID = "11fab82a-426e-513e-abf3-528d99ef2b87"
DB_SANDBOX_AGENT_ID = "be4d6c18-629d-5b1c-8c46-f82c00db4995"
VISUAL_REGRESSION_AGENT_ID = "20a74467-d633-5016-b210-adf769b2df9c"
BROWSER_AGENT_ID = "c3a1b2d4-e5f6-5a7b-8c9d-0e1f2a3b4c5d"
MULTI_LANGUAGE_EXECUTOR_AGENT_ID = "d4b2c3e5-f6a7-5b8c-9d0e-1f2a3b4c5d6e"
SECRET_SCANNER_AGENT_ID = "1021c65c-d2bf-54ff-823a-897f9deb1029"
JSON_SCHEMA_VALIDATOR_AGENT_ID = "1b0b5820-b796-53cc-8d31-5e336d86d875"
GIT_DIFF_ANALYZER_AGENT_ID = "8ac84144-4fd1-5883-bfad-e7b64d729b8f"
LIGHTHOUSE_AUDITOR_AGENT_ID = "6047127b-e49a-51c0-81d7-7934c0be424d"
ACCESSIBILITY_AUDITOR_AGENT_ID = "41e95324-2480-5e53-9414-302d55673d50"
SECURITY_HEADERS_GRADER_AGENT_ID = "33171c82-b9a0-5cef-b867-c7da3889cff1"
BROKEN_LINK_CRAWLER_AGENT_ID = "79199276-9dc3-593d-9d85-26241365f292"
PDF_DOCUMENT_PARSER_AGENT_ID = "c569490b-c886-5c94-b22b-192027d8c485"
WEB_SEARCH_AGENT_ID = "7d5f4e06-60b5-5950-a885-eaef04cf0b33"
DOCS_GROUNDER_AGENT_ID = "7a93b924-e981-5d38-8e63-e117ba691aac"
SAST_SCANNER_AGENT_ID = "91d229dc-1f37-5044-aaa2-f157e9425159"
STRIPE_WEBHOOK_DEBUGGER_AGENT_ID = "0dd11350-0307-5900-ac19-71105117a9c9"
LOAD_TESTER_AGENT_ID = "38143c50-4484-595c-827f-629d3c877f7e"
CI_FAILURE_REPRODUCER_AGENT_ID = "fec9fdac-4685-579f-b26f-82119124c73e"
DOCKERFILE_ANALYZER_AGENT_ID = "e91f9b2f-f695-5890-b1f5-a9156c1b9a54"
OPENAPI_VALIDATOR_AGENT_ID = "ce4230c8-1f16-5820-9852-7511b34603d7"
COVERAGE_RUNNER_AGENT_ID = "20e5454b-5953-5b20-a993-1dfc92c20cfb"
SSL_CERTIFICATE_DECODER_AGENT_ID = "fefbff0b-8343-5a58-8aec-9d1579188919"
DIFF_ANALYZER_AGENT_ID = "8b980edd-6583-51d2-b351-d2afe1d57ff2"
K8S_MANIFEST_VALIDATOR_AGENT_ID = "6086b2ad-0a55-58e5-b504-49968379b623"
ARCHIVE_INSPECTOR_AGENT_ID = "9713a29a-1817-5548-b439-0cd4f4efdcb1"
UNICODE_INSPECTOR_AGENT_ID = "65fbf6ec-ff53-5f72-95e0-88ae2070c3d9"
TERRAFORM_PLAN_ANALYZER_AGENT_ID = "989f2964-fadd-5ce0-9afc-2183c08fb9f9"
LIVE_SANDBOX_AGENT_ID = "3354f7c4-bb9d-55e2-8e8c-df67a64f57a2"
# 2026-05-18 — six new agents addressing catalog gaps surfaced in the
# 2026-05-18 test report (regex_tester, jwt_validator, sbom_generator,
# pypi_metadata, github_releases, hcl_terraform_analyzer).
REGEX_TESTER_AGENT_ID = "36ae44b0-895b-5ef7-bc1f-1ecf08fce3ee"
JWT_VALIDATOR_AGENT_ID = "96c86f16-16e6-51bb-9332-eae0cfef33ba"
SBOM_GENERATOR_AGENT_ID = "f14ab9fe-cb29-5a86-a7a2-f42de85ff99a"
PYPI_METADATA_AGENT_ID = "9dd0e946-974c-5f0b-a59d-db15cc503ec6"
GITHUB_RELEASES_AGENT_ID = "c199ff57-599b-5e90-b908-994a98b57571"
HCL_TERRAFORM_ANALYZER_AGENT_ID = "9ad19220-a46b-54b0-9190-dded9399b45c"

BUILTIN_INTERNAL_ENDPOINTS: dict[str, str] = {
    QUALITY_JUDGE_AGENT_ID: "internal://quality-judge",
    CVELOOKUP_AGENT_ID: "internal://cve-lookup",
    PYTHON_EXECUTOR_AGENT_ID: "internal://python-executor",
    DNS_INSPECTOR_AGENT_ID: "internal://dns_inspector",
    DEPENDENCY_AUDITOR_AGENT_ID: "internal://dependency_auditor",
    DB_SANDBOX_AGENT_ID: "internal://db_sandbox",
    VISUAL_REGRESSION_AGENT_ID: "internal://visual_regression",
    BROWSER_AGENT_ID: "internal://browser_agent",
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID: "internal://multi_language_executor",
    SECRET_SCANNER_AGENT_ID: "internal://secret_scanner",
    LIGHTHOUSE_AUDITOR_AGENT_ID: "internal://lighthouse_auditor",
    ACCESSIBILITY_AUDITOR_AGENT_ID: "internal://accessibility_auditor",
    SECURITY_HEADERS_GRADER_AGENT_ID: "internal://security_headers_grader",
    BROKEN_LINK_CRAWLER_AGENT_ID: "internal://broken_link_crawler",
    PDF_DOCUMENT_PARSER_AGENT_ID: "internal://pdf_document_parser",
    WEB_SEARCH_AGENT_ID: "internal://web_search",
    DOCS_GROUNDER_AGENT_ID: "internal://docs_grounder",
    SAST_SCANNER_AGENT_ID: "internal://sast_scanner",
    STRIPE_WEBHOOK_DEBUGGER_AGENT_ID: "internal://stripe_webhook_debugger",
    LOAD_TESTER_AGENT_ID: "internal://load_tester",
    CI_FAILURE_REPRODUCER_AGENT_ID: "internal://ci_failure_reproducer",
    DOCKERFILE_ANALYZER_AGENT_ID: "internal://dockerfile_analyzer",
    OPENAPI_VALIDATOR_AGENT_ID: "internal://openapi_validator",
    COVERAGE_RUNNER_AGENT_ID: "internal://coverage_runner",
    SSL_CERTIFICATE_DECODER_AGENT_ID: "internal://ssl_certificate_decoder",
    DIFF_ANALYZER_AGENT_ID: "internal://diff_analyzer",
    K8S_MANIFEST_VALIDATOR_AGENT_ID: "internal://k8s_manifest_validator",
    ARCHIVE_INSPECTOR_AGENT_ID: "internal://archive_inspector",
    UNICODE_INSPECTOR_AGENT_ID: "internal://unicode_inspector",
    TERRAFORM_PLAN_ANALYZER_AGENT_ID: "internal://terraform_plan_analyzer",
    LIVE_SANDBOX_AGENT_ID: "internal://live_sandbox",
    REGEX_TESTER_AGENT_ID: "internal://regex_tester",
    JWT_VALIDATOR_AGENT_ID: "internal://jwt_validator",
    SBOM_GENERATOR_AGENT_ID: "internal://sbom_generator",
    PYPI_METADATA_AGENT_ID: "internal://pypi_metadata",
    GITHUB_RELEASES_AGENT_ID: "internal://github_releases",
    HCL_TERRAFORM_ANALYZER_AGENT_ID: "internal://hcl_terraform_analyzer",
}

BUILTIN_LEGACY_ROUTE_ENDPOINTS: dict[str, str] = {
    QUALITY_JUDGE_AGENT_ID: f"{SERVER_BASE_URL}/agents/quality-judge",
    CVELOOKUP_AGENT_ID: f"{SERVER_BASE_URL}/agents/cve-lookup",
    PYTHON_EXECUTOR_AGENT_ID: f"{SERVER_BASE_URL}/agents/python-executor",
}

BUILTIN_ENDPOINT_TO_AGENT_ID: dict[str, str] = {}
for _agent_id, _endpoint in BUILTIN_INTERNAL_ENDPOINTS.items():
    BUILTIN_ENDPOINT_TO_AGENT_ID[normalize_endpoint_ref(_endpoint)] = _agent_id
    _legacy = BUILTIN_LEGACY_ROUTE_ENDPOINTS.get(_agent_id)
    if _legacy:
        BUILTIN_ENDPOINT_TO_AGENT_ID[normalize_endpoint_ref(_legacy)] = _agent_id

BUILTIN_AGENT_IDS = frozenset(BUILTIN_INTERNAL_ENDPOINTS.keys())

# Sunset agents: returned in catalog listings (so callers see a clear
# "sunsetted" status instead of a silent disappearance) but excluded from
# CURATED_PUBLIC_BUILTIN_AGENT_IDS so they aren't recommended by
# auto-invoke / search. New IDs added here MUST also be removed from
# CURATED_PUBLIC_BUILTIN_AGENT_IDS (the sanity assert below catches drift).
#
# 2026-05-17: docs_grounder sunsetted — the 2026-05-17 extensive test
# report observed persistent 502 agent.endpoint_misconfigured / live-data
# errors. The internal endpoint remains wired so existing callers don't
# 410, but the agent is hidden from the curated catalog. Re-list once the
# upstream docs source is restored.
#
# 2026-05-20: catalog-quality cull — eleven builtins sunsetted to raise the
# average quality bar of the curated public catalog. Each fails the
# "do something Claude can't do in a chat session" rule, has a known
# honesty bug, or is dominated by a sibling agent. Internal endpoints
# stay wired so old job IDs / receipts continue to resolve. Reasoning:
#   - diff_analyzer: pure LLM wrapper around git diff (Claude does this natively).
#   - unicode_inspector: confusable/homoglyph detection on text; known
#     contradiction bug between has_mixed_scripts and scripts_detected (M1 deferred).
#   - regex_tester: marginal value; regex eval is trivially Claude-doable
#     and the catastrophic-backtracking guard is the only real differentiator.
#   - ssl_certificate_decoder: overlaps dns_inspector (which already
#     surfaces TLS cert metadata for the same hostname).
#   - pypi_metadata: thin wrapper over PyPI JSON API; integrators can
#     curl this directly without the platform fee.
#   - github_releases: thin wrapper over GitHub Releases API; same as above.
#   - security_headers_grader: live HTTP header grade; Claude+curl does this
#     fine and dependency_auditor surfaces the same finding when relevant.
#   - sbom_generator: format-converter (lockfile → CycloneDX/SPDX); mechanical
#     and dominated by dependency_auditor for the security use case.
#   - web_search: undifferentiated wrapper around a search backend.
#   - visual_regression: pixel-diff is useful but the agent has never
#     graduated past stability concerns and overlaps lighthouse_auditor screenshots.
#   - archive_inspector: zip-slip guard is real but the inspect step is
#     trivially Claude-doable for the audit use case.
SUNSET_DEPRECATED_AGENT_IDS: frozenset[str] = frozenset({
    DOCS_GROUNDER_AGENT_ID,
    DIFF_ANALYZER_AGENT_ID,
    UNICODE_INSPECTOR_AGENT_ID,
    REGEX_TESTER_AGENT_ID,
    SSL_CERTIFICATE_DECODER_AGENT_ID,
    PYPI_METADATA_AGENT_ID,
    GITHUB_RELEASES_AGENT_ID,
    SECURITY_HEADERS_GRADER_AGENT_ID,
    SBOM_GENERATOR_AGENT_ID,
    WEB_SEARCH_AGENT_ID,
    VISUAL_REGRESSION_AGENT_ID,
    ARCHIVE_INSPECTOR_AGENT_ID,
})

# The public catalog: agents that give a coding-agent integrator a primitive
# they cannot trivially build themselves — isolation (sandboxes), live
# external data (CVE/DNS/HTTP), or specialist runtimes (browser, pixel diff).
# Sunsetted agents are explicitly NOT listed here; see SUNSET_DEPRECATED_AGENT_IDS
# for the cull list and reasoning. 2026-05-20 cull dropped 11 builtins.
CURATED_PUBLIC_BUILTIN_AGENT_IDS = frozenset(
    {
        CVELOOKUP_AGENT_ID,
        PYTHON_EXECUTOR_AGENT_ID,
        DNS_INSPECTOR_AGENT_ID,
        DEPENDENCY_AUDITOR_AGENT_ID,
        DB_SANDBOX_AGENT_ID,
        BROWSER_AGENT_ID,
        MULTI_LANGUAGE_EXECUTOR_AGENT_ID,
        SECRET_SCANNER_AGENT_ID,
        LIGHTHOUSE_AUDITOR_AGENT_ID,
        ACCESSIBILITY_AUDITOR_AGENT_ID,
        BROKEN_LINK_CRAWLER_AGENT_ID,
        PDF_DOCUMENT_PARSER_AGENT_ID,
        SAST_SCANNER_AGENT_ID,
        STRIPE_WEBHOOK_DEBUGGER_AGENT_ID,
        LOAD_TESTER_AGENT_ID,
        CI_FAILURE_REPRODUCER_AGENT_ID,
        DOCKERFILE_ANALYZER_AGENT_ID,
        OPENAPI_VALIDATOR_AGENT_ID,
        COVERAGE_RUNNER_AGENT_ID,
        K8S_MANIFEST_VALIDATOR_AGENT_ID,
        TERRAFORM_PLAN_ANALYZER_AGENT_ID,
        LIVE_SANDBOX_AGENT_ID,
        JWT_VALIDATOR_AGENT_ID,
        HCL_TERRAFORM_ANALYZER_AGENT_ID,
    }
)
# Sanity: a sunset agent must never accidentally re-appear in the public set.
assert not (SUNSET_DEPRECATED_AGENT_IDS & CURATED_PUBLIC_BUILTIN_AGENT_IDS), (
    "Sunset agents must not be in the curated public catalog"
)
CURATED_BUILTIN_AGENT_IDS = frozenset(
    set(CURATED_PUBLIC_BUILTIN_AGENT_IDS)
    | set(SUNSET_DEPRECATED_AGENT_IDS)
    | {QUALITY_JUDGE_AGENT_ID}
)
# Public-catalog filter (list_agents / search / auto-hire / MCP manifests) uses
# CURATED_PUBLIC. Registry seeding + spec generation use CURATED_BUILTIN so old
# job IDs and receipts still resolve cleanly.


# Platform-subsidized gateway agents — exposed at $0.00 so a first-time
# Aztea caller can see one finding before any decision about whether to
# trust the rest of the catalog. The pricing knob lives in each spec file
# (price_per_call_usd = 0.0) and, for variable-priced CVE Lookup, in
# pricing_overlay.py. This set is informational so future readers can
# locate every member by id without grepping spec text. Adding an agent
# here without zeroing its spec price is a documentation bug, not a
# behavior change — the spec price is authoritative.
GATEWAY_FREE_TIER_AGENT_IDS = frozenset(
    {
        SECRET_SCANNER_AGENT_ID,
        DOCKERFILE_ANALYZER_AGENT_ID,
        CVELOOKUP_AGENT_ID,
    }
)


BUILTIN_WORKER_OWNER_ID = "system:builtin-worker"
SYSTEM_USERNAME = "system"
SYSTEM_USER_EMAIL = "system@aztea.internal"


# Agents that the OSS-mode runtime should prefer to call against the hosted
# aztea.ai API instead of dispatching locally. Currently only the quality
# judge — its prompt + model are proprietary and shouldn't run locally.
PREFER_HOSTED_AGENT_IDS = frozenset(
    {
        QUALITY_JUDGE_AGENT_ID,
    }
)
# Sanity: no deprecated agent should be in the prefer-hosted set. This
# fires at import time so a regressed addition is caught loudly.
assert not (PREFER_HOSTED_AGENT_IDS & SUNSET_DEPRECATED_AGENT_IDS), (
    "PREFER_HOSTED_AGENT_IDS must not include sunset agents — they should "
    "either be removed from sunset or removed from prefer-hosted."
)


def agent_id_to_slug(agent_id: str) -> str | None:
    """Map a built-in agent UUID to its hosted-API slug.

    Slug is derived from the `internal://<slug>` endpoint registration. Returns
    None for unknown / non-builtin agent IDs so the caller can skip the
    hosted call.
    """
    endpoint = BUILTIN_INTERNAL_ENDPOINTS.get(agent_id)
    if not endpoint:
        return None
    if not endpoint.startswith("internal://"):
        return None
    return endpoint.removeprefix("internal://").strip() or None
