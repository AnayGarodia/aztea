"""Deterministic IDs, endpoint maps, and curated visibility for built-in agents."""

from __future__ import annotations

import os

SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "http://localhost:8000").rstrip("/")


def normalize_endpoint_ref(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


FINANCIAL_AGENT_ID = "b7741251-d7ac-5423-b57d-8e12cd80885f"
CODEREVIEW_AGENT_ID = "8cea848f-a165-5d6c-b1a0-7d14fff77d14"
WIKI_AGENT_ID = "9a175aa2-8ffd-52f7-aae0-5a33fc88db83"
CVELOOKUP_AGENT_ID = "a3e239dd-ea92-556b-9c95-0a213a3daf59"
QUALITY_JUDGE_AGENT_ID = "9cf0d9d0-4a10-58c9-b97a-6b5f81b1cf33"
IMAGE_GENERATOR_AGENT_ID = "4fb167bd-b474-5ea5-bd5c-8976dfe799ae"
VIDEO_STORYBOARD_AGENT_ID = "c12994de-cde9-514a-9c07-a3833b25bb1f"
ARXIV_RESEARCH_AGENT_ID = "9e673f6e-9115-516f-b41b-5af8bcbf15bd"
PYTHON_EXECUTOR_AGENT_ID = "040dc3f5-afe7-5db7-b253-4936090cc7af"
WEB_RESEARCHER_AGENT_ID = "32cd7b5c-44d0-5259-bb02-1bbc612e92d7"
HN_DIGEST_AGENT_ID = "31cc3a99-eca6-5202-96d4-8366f426ae1d"
DNS_INSPECTOR_AGENT_ID = "3d677381-791c-5e83-8e66-5b77d0e43e2e"
DEPENDENCY_AUDITOR_AGENT_ID = "11fab82a-426e-513e-abf3-528d99ef2b87"
MULTI_FILE_EXECUTOR_AGENT_ID = "ea95cdec-32c1-5a2b-a032-3e7061abf3a4"
LINTER_AGENT_ID = "7ec4c987-9a7e-5af8-984f-7b8ad0ad0536"
SHELL_EXECUTOR_AGENT_ID = "6bd98167-e010-5604-8c76-6ed1b92698f1"
TYPE_CHECKER_AGENT_ID = "5b140628-52a8-565b-8599-b1c3e402b02d"
DB_SANDBOX_AGENT_ID = "be4d6c18-629d-5b1c-8c46-f82c00db4995"
VISUAL_REGRESSION_AGENT_ID = "20a74467-d633-5016-b210-adf769b2df9c"
LIVE_ENDPOINT_TESTER_AGENT_ID = "8af9fc34-ec0c-5732-b0e0-4e4efdff749c"
BROWSER_AGENT_ID = "c3a1b2d4-e5f6-5a7b-8c9d-0e1f2a3b4c5d"
MULTI_LANGUAGE_EXECUTOR_AGENT_ID = "d4b2c3e5-f6a7-5b8c-9d0e-1f2a3b4c5d6e"
SEMANTIC_CODEBASE_SEARCH_AGENT_ID = "e5c3d4f6-a7b8-5c9d-0e1f-2a3b4c5d6e7f"
AI_RED_TEAMER_AGENT_ID = "f6d4e5a7-b8c9-5d0e-1f2a-3b4c5d6e7f8a"
SECRET_SCANNER_AGENT_ID = "1021c65c-d2bf-54ff-823a-897f9deb1029"
JSON_SCHEMA_VALIDATOR_AGENT_ID = "1b0b5820-b796-53cc-8d31-5e336d86d875"
REGEX_TESTER_AGENT_ID = "36ae44b0-895b-5ef7-bc1f-1ecf08fce3ee"
SQL_EXPLAINER_AGENT_ID = "91258740-dd32-51b6-be91-a7638fae190f"
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
JWT_DEBUGGER_AGENT_ID = "e4aa9794-c37a-5e2f-992e-b325fabb2caf"
DOCKERFILE_ANALYZER_AGENT_ID = "e91f9b2f-f695-5890-b1f5-a9156c1b9a54"
OPENAPI_VALIDATOR_AGENT_ID = "ce4230c8-1f16-5820-9852-7511b34603d7"
COVERAGE_RUNNER_AGENT_ID = "20e5454b-5953-5b20-a993-1dfc92c20cfb"
EMAIL_DELIVERABILITY_CHECKER_AGENT_ID = "37d58c2f-7624-529c-9bcd-f0f5e44f1e12"

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
    JWT_DEBUGGER_AGENT_ID: "internal://jwt_debugger",
    DOCKERFILE_ANALYZER_AGENT_ID: "internal://dockerfile_analyzer",
    OPENAPI_VALIDATOR_AGENT_ID: "internal://openapi_validator",
    COVERAGE_RUNNER_AGENT_ID: "internal://coverage_runner",
    EMAIL_DELIVERABILITY_CHECKER_AGENT_ID: "internal://email_deliverability_checker",
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

# Agents demoted from the public catalog after the 2026-05-07 power-user eval.
# They remain resolvable through historical job and receipt endpoints, but
# non-admin callers can no longer discover or hire them. Hard-delete after the
# platform-wide sunset window:
#   - Pure code wrappers (linter / type_checker / regex_tester /
#     json_schema_validator / git_diff_analyzer / shell_executor /
#     multi_file_executor): a coding agent runs these locally in 1-20 lines.
#   - LLM-only wrappers (code_review / arxiv / wiki / web_researcher / financial /
#     ai_red_teamer): no live data Claude can't reach via WebFetch+training.
#   - Wrong surface for a coding marketplace (image_generator / semantic_search:
#     the latter is dominated by Grep/Glob/Read).
SUNSET_DEPRECATED_AGENT_IDS = frozenset(
    {
        ARXIV_RESEARCH_AGENT_ID,
        MULTI_FILE_EXECUTOR_AGENT_ID,
        LINTER_AGENT_ID,
        SHELL_EXECUTOR_AGENT_ID,
        TYPE_CHECKER_AGENT_ID,
        SEMANTIC_CODEBASE_SEARCH_AGENT_ID,
        IMAGE_GENERATOR_AGENT_ID,
        FINANCIAL_AGENT_ID,
        # Suspended in prod DB; treated as sunset so every surface
        # (list_agents, /health, session_audit, search) agrees.
        LIVE_ENDPOINT_TESTER_AGENT_ID,
        SQL_EXPLAINER_AGENT_ID,
    }
)

# The public catalog: agents that give a coding-agent integrator a primitive
# they cannot trivially build themselves — isolation (sandboxes), live
# external data (CVE/DNS/HTTP), or specialist runtimes (browser, pixel diff).
CURATED_PUBLIC_BUILTIN_AGENT_IDS = frozenset(
    {
        CVELOOKUP_AGENT_ID,
        PYTHON_EXECUTOR_AGENT_ID,
        DNS_INSPECTOR_AGENT_ID,
        DEPENDENCY_AUDITOR_AGENT_ID,
        DB_SANDBOX_AGENT_ID,
        BROWSER_AGENT_ID,
        VISUAL_REGRESSION_AGENT_ID,
        MULTI_LANGUAGE_EXECUTOR_AGENT_ID,
        SECRET_SCANNER_AGENT_ID,
        LIGHTHOUSE_AUDITOR_AGENT_ID,
        ACCESSIBILITY_AUDITOR_AGENT_ID,
        SECURITY_HEADERS_GRADER_AGENT_ID,
        BROKEN_LINK_CRAWLER_AGENT_ID,
        PDF_DOCUMENT_PARSER_AGENT_ID,
        WEB_SEARCH_AGENT_ID,
        DOCS_GROUNDER_AGENT_ID,
        SAST_SCANNER_AGENT_ID,
        STRIPE_WEBHOOK_DEBUGGER_AGENT_ID,
        LOAD_TESTER_AGENT_ID,
        CI_FAILURE_REPRODUCER_AGENT_ID,
        JWT_DEBUGGER_AGENT_ID,
        DOCKERFILE_ANALYZER_AGENT_ID,
        OPENAPI_VALIDATOR_AGENT_ID,
        COVERAGE_RUNNER_AGENT_ID,
        EMAIL_DELIVERABILITY_CHECKER_AGENT_ID,
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

BUILTIN_WORKER_OWNER_ID = "system:builtin-worker"
SYSTEM_USERNAME = "system"
SYSTEM_USER_EMAIL = "system@aztea.internal"
