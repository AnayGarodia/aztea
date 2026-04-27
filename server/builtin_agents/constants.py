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
GITHUB_FETCHER_AGENT_ID = "5896576f-bbe6-59e4-83c1-5106002e7d10"
HN_DIGEST_AGENT_ID = "31cc3a99-eca6-5202-96d4-8366f426ae1d"
DNS_INSPECTOR_AGENT_ID = "3d677381-791c-5e83-8e66-5b77d0e43e2e"
PR_REVIEWER_AGENT_ID = "3e133b66-3bc6-5003-9b64-3284b28a60c6"
TEST_GENERATOR_AGENT_ID = "f515323c-7df2-5742-ac06-bc38b59a40cb"
SPEC_WRITER_AGENT_ID = "ce9504a3-74c8-51a5-913e-6ae55787abc8"
DEPENDENCY_AUDITOR_AGENT_ID = "11fab82a-426e-513e-abf3-528d99ef2b87"
MULTI_FILE_EXECUTOR_AGENT_ID = "ea95cdec-32c1-5a2b-a032-3e7061abf3a4"
CHANGELOG_AGENT_ID = "48c24ce5-d9cb-5f76-9e2f-fce1878f8c4c"
PACKAGE_FINDER_AGENT_ID = "d11ddab1-bcca-55de-8b00-c9efadc69c79"
LINTER_AGENT_ID = "7ec4c987-9a7e-5af8-984f-7b8ad0ad0536"
SHELL_EXECUTOR_AGENT_ID = "6bd98167-e010-5604-8c76-6ed1b92698f1"
TYPE_CHECKER_AGENT_ID = "5b140628-52a8-565b-8599-b1c3e402b02d"

BUILTIN_INTERNAL_ENDPOINTS: dict[str, str] = {
    FINANCIAL_AGENT_ID: "internal://financial",
    CODEREVIEW_AGENT_ID: "internal://code-review",
    WIKI_AGENT_ID: "internal://wiki",
    QUALITY_JUDGE_AGENT_ID: "internal://quality-judge",
    CVELOOKUP_AGENT_ID: "internal://cve-lookup",
    IMAGE_GENERATOR_AGENT_ID: "internal://image-generator",
    VIDEO_STORYBOARD_AGENT_ID: "internal://video-storyboard-generator",
    ARXIV_RESEARCH_AGENT_ID: "internal://arxiv-research",
    PYTHON_EXECUTOR_AGENT_ID: "internal://python-executor",
    WEB_RESEARCHER_AGENT_ID: "internal://web-researcher",
    GITHUB_FETCHER_AGENT_ID: "internal://github_fetcher",
    HN_DIGEST_AGENT_ID: "internal://hn_digest",
    DNS_INSPECTOR_AGENT_ID: "internal://dns_inspector",
    PR_REVIEWER_AGENT_ID: "internal://pr_reviewer",
    TEST_GENERATOR_AGENT_ID: "internal://test_generator",
    SPEC_WRITER_AGENT_ID: "internal://spec_writer",
    DEPENDENCY_AUDITOR_AGENT_ID: "internal://dependency_auditor",
    MULTI_FILE_EXECUTOR_AGENT_ID: "internal://multi_file_executor",
    CHANGELOG_AGENT_ID: "internal://changelog_agent",
    PACKAGE_FINDER_AGENT_ID: "internal://package_finder",
    LINTER_AGENT_ID: "internal://linter_agent",
    SHELL_EXECUTOR_AGENT_ID: "internal://shell_executor",
    TYPE_CHECKER_AGENT_ID: "internal://type_checker",
}

BUILTIN_LEGACY_ROUTE_ENDPOINTS: dict[str, str] = {
    FINANCIAL_AGENT_ID: f"{SERVER_BASE_URL}/agents/financial",
    CODEREVIEW_AGENT_ID: f"{SERVER_BASE_URL}/agents/code-review",
    WIKI_AGENT_ID: f"{SERVER_BASE_URL}/agents/wiki",
    QUALITY_JUDGE_AGENT_ID: f"{SERVER_BASE_URL}/agents/quality-judge",
    CVELOOKUP_AGENT_ID: f"{SERVER_BASE_URL}/agents/cve-lookup",
    IMAGE_GENERATOR_AGENT_ID: f"{SERVER_BASE_URL}/agents/image-generator",
    VIDEO_STORYBOARD_AGENT_ID: f"{SERVER_BASE_URL}/agents/video-storyboard-generator",
    ARXIV_RESEARCH_AGENT_ID: f"{SERVER_BASE_URL}/agents/arxiv-research",
    PYTHON_EXECUTOR_AGENT_ID: f"{SERVER_BASE_URL}/agents/python-executor",
    WEB_RESEARCHER_AGENT_ID: f"{SERVER_BASE_URL}/agents/web-researcher",
}

BUILTIN_ENDPOINT_TO_AGENT_ID: dict[str, str] = {}
for _agent_id, _endpoint in BUILTIN_INTERNAL_ENDPOINTS.items():
    BUILTIN_ENDPOINT_TO_AGENT_ID[normalize_endpoint_ref(_endpoint)] = _agent_id
    _legacy = BUILTIN_LEGACY_ROUTE_ENDPOINTS.get(_agent_id)
    if _legacy:
        BUILTIN_ENDPOINT_TO_AGENT_ID[normalize_endpoint_ref(_legacy)] = _agent_id
BUILTIN_ENDPOINT_TO_AGENT_ID[normalize_endpoint_ref(f"{SERVER_BASE_URL}/analyze")] = FINANCIAL_AGENT_ID

BUILTIN_AGENT_IDS = frozenset(BUILTIN_INTERNAL_ENDPOINTS.keys())

# LLM-only wrappers that are kept for backward compatibility but hidden from the
# public marketplace. Callers of these agents receive Deprecation + Sunset headers
# so they can migrate to direct Claude prompts before the sunset date.
DEPRECATED_BUILTIN_AGENT_IDS = frozenset(
    {
        GITHUB_FETCHER_AGENT_ID,
        PR_REVIEWER_AGENT_ID,
        TEST_GENERATOR_AGENT_ID,
        SPEC_WRITER_AGENT_ID,
        CHANGELOG_AGENT_ID,
        PACKAGE_FINDER_AGENT_ID,
    }
)
# Sunset date: 90 days after the Phase 3 cleanup commit (2026-04-27)
DEPRECATED_AGENTS_SUNSET_DATE = "2026-07-26"
CURATED_PUBLIC_BUILTIN_AGENT_IDS = frozenset(
    {
        # Real-tool agents: perform live external work Claude cannot do in a chat session
        CVELOOKUP_AGENT_ID,
        ARXIV_RESEARCH_AGENT_ID,
        PYTHON_EXECUTOR_AGENT_ID,
        WEB_RESEARCHER_AGENT_ID,
        IMAGE_GENERATOR_AGENT_ID,
        CODEREVIEW_AGENT_ID,
        DNS_INSPECTOR_AGENT_ID,
        DEPENDENCY_AUDITOR_AGENT_ID,
        MULTI_FILE_EXECUTOR_AGENT_ID,
        LINTER_AGENT_ID,
        SHELL_EXECUTOR_AGENT_ID,
        TYPE_CHECKER_AGENT_ID,
        # LLM-only wrappers (github_fetcher, pr_reviewer, test_generator, spec_writer,
        # changelog_agent, package_finder) intentionally excluded — they add no value
        # over a direct Claude chat session and erode marketplace trust.
        # They remain in BUILTIN_INTERNAL_ENDPOINTS for backward compatibility.
    }
)
CURATED_BUILTIN_AGENT_IDS = frozenset(set(CURATED_PUBLIC_BUILTIN_AGENT_IDS) | {QUALITY_JUDGE_AGENT_ID})

BUILTIN_WORKER_OWNER_ID = "system:builtin-worker"
SYSTEM_USERNAME = "system"
SYSTEM_USER_EMAIL = "system@aztea.internal"
