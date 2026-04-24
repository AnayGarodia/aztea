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
CURATED_PUBLIC_BUILTIN_AGENT_IDS = frozenset(
    {
        FINANCIAL_AGENT_ID,
        WIKI_AGENT_ID,
        CVELOOKUP_AGENT_ID,
        ARXIV_RESEARCH_AGENT_ID,
        PYTHON_EXECUTOR_AGENT_ID,
        WEB_RESEARCHER_AGENT_ID,
        IMAGE_GENERATOR_AGENT_ID,
        CODEREVIEW_AGENT_ID,
    }
)
CURATED_BUILTIN_AGENT_IDS = frozenset(set(CURATED_PUBLIC_BUILTIN_AGENT_IDS) | {QUALITY_JUDGE_AGENT_ID})

BUILTIN_WORKER_OWNER_ID = "system:builtin-worker"
SYSTEM_USERNAME = "system"
SYSTEM_USER_EMAIL = "system@aztea.internal"
