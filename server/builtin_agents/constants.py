"""Deterministic IDs, endpoint maps, and curated visibility for built-in agents."""

from __future__ import annotations

import os

SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "http://localhost:8000").rstrip("/")


def normalize_endpoint_ref(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


FINANCIAL_AGENT_ID = "b7741251-d7ac-5423-b57d-8e12cd80885f"
CODEREVIEW_AGENT_ID = "8cea848f-a165-5d6c-b1a0-7d14fff77d14"
TEXTINTEL_AGENT_ID = "3daebf56-1873-5e7c-ba4f-7e69c51aefac"
WIKI_AGENT_ID = "9a175aa2-8ffd-52f7-aae0-5a33fc88db83"
NEGOTIATION_AGENT_ID = "39b2867f-4910-5b5b-9492-bbb3f4ae4a06"
SCENARIO_AGENT_ID = "d2e672ae-2a2e-52f9-8e60-10a644ba49bb"
PRODUCT_AGENT_ID = "6dd1d7ff-d838-5d35-b633-da89602fea7e"
PORTFOLIO_AGENT_ID = "4fa63abf-fea3-513b-9203-bc09ff668a44"
RESUME_AGENT_ID = "17076c9b-ae5c-534d-9054-705fc9afc4b3"
SQLBUILDER_AGENT_ID = "b1de8c04-f82c-506c-9305-d67dfaea2e4f"
DATAINSIGHTS_AGENT_ID = "51214278-5a31-5de8-8514-5a2c07ccfa4d"
EMAILWRITER_AGENT_ID = "07891578-d49e-54b2-9297-db4a453f1fbb"
SECRETS_AGENT_ID = "b52b13ea-d7f7-5030-89b7-eed22dc0a9fa"
STATICANALYSIS_AGENT_ID = "e2b69985-1f53-5ae3-aba6-df38d5f024da"
DEPSCANNER_AGENT_ID = "9adba8e2-fa19-5160-9e67-143e0811ba91"
CVELOOKUP_AGENT_ID = "a3e239dd-ea92-556b-9c95-0a213a3daf59"
QUALITY_JUDGE_AGENT_ID = "9cf0d9d0-4a10-58c9-b97a-6b5f81b1cf33"
SYSTEM_DESIGN_AGENT_ID = "eda2e80c-78a1-5a94-ae2b-e450858a7efa"
INCIDENT_RESPONSE_AGENT_ID = "5cceca4c-85f2-5b2d-bc06-3b352aaf0c33"
HEALTHCARE_EXPERT_AGENT_ID = "40d9012b-f611-502f-a73b-ef631efed163"
IMAGE_GENERATOR_AGENT_ID = "4fb167bd-b474-5ea5-bd5c-8976dfe799ae"
VIDEO_STORYBOARD_AGENT_ID = "c12994de-cde9-514a-9c07-a3833b25bb1f"
ARXIV_RESEARCH_AGENT_ID = "9e673f6e-9115-516f-b41b-5af8bcbf15bd"
PYTHON_EXECUTOR_AGENT_ID = "040dc3f5-afe7-5db7-b253-4936090cc7af"
WEB_RESEARCHER_AGENT_ID = "32cd7b5c-44d0-5259-bb02-1bbc612e92d7"

BUILTIN_INTERNAL_ENDPOINTS: dict[str, str] = {
    FINANCIAL_AGENT_ID: "internal://financial",
    CODEREVIEW_AGENT_ID: "internal://code-review",
    TEXTINTEL_AGENT_ID: "internal://text-intel",
    WIKI_AGENT_ID: "internal://wiki",
    NEGOTIATION_AGENT_ID: "internal://negotiation",
    SCENARIO_AGENT_ID: "internal://scenario",
    PRODUCT_AGENT_ID: "internal://product-strategy",
    PORTFOLIO_AGENT_ID: "internal://portfolio",
    QUALITY_JUDGE_AGENT_ID: "internal://quality-judge",
    SQLBUILDER_AGENT_ID: "internal://sql-builder",
    DATAINSIGHTS_AGENT_ID: "internal://data-insights",
    SECRETS_AGENT_ID: "internal://secrets-detection",
    STATICANALYSIS_AGENT_ID: "internal://static-analysis",
    DEPSCANNER_AGENT_ID: "internal://dependency-scanner",
    CVELOOKUP_AGENT_ID: "internal://cve-lookup",
    SYSTEM_DESIGN_AGENT_ID: "internal://system-design-reviewer",
    INCIDENT_RESPONSE_AGENT_ID: "internal://incident-response-commander",
    HEALTHCARE_EXPERT_AGENT_ID: "internal://healthcare-expert",
    IMAGE_GENERATOR_AGENT_ID: "internal://image-generator",
    VIDEO_STORYBOARD_AGENT_ID: "internal://video-storyboard-generator",
    ARXIV_RESEARCH_AGENT_ID: "internal://arxiv-research",
    PYTHON_EXECUTOR_AGENT_ID: "internal://python-executor",
    WEB_RESEARCHER_AGENT_ID: "internal://web-researcher",
}

BUILTIN_LEGACY_ROUTE_ENDPOINTS: dict[str, str] = {
    FINANCIAL_AGENT_ID: f"{SERVER_BASE_URL}/agents/financial",
    CODEREVIEW_AGENT_ID: f"{SERVER_BASE_URL}/agents/code-review",
    TEXTINTEL_AGENT_ID: f"{SERVER_BASE_URL}/agents/text-intel",
    WIKI_AGENT_ID: f"{SERVER_BASE_URL}/agents/wiki",
    NEGOTIATION_AGENT_ID: f"{SERVER_BASE_URL}/agents/negotiation",
    SCENARIO_AGENT_ID: f"{SERVER_BASE_URL}/agents/scenario",
    PRODUCT_AGENT_ID: f"{SERVER_BASE_URL}/agents/product-strategy",
    PORTFOLIO_AGENT_ID: f"{SERVER_BASE_URL}/agents/portfolio",
    QUALITY_JUDGE_AGENT_ID: f"{SERVER_BASE_URL}/agents/quality-judge",
    SQLBUILDER_AGENT_ID: f"{SERVER_BASE_URL}/agents/sql-builder",
    DATAINSIGHTS_AGENT_ID: f"{SERVER_BASE_URL}/agents/data-insights",
    SECRETS_AGENT_ID: f"{SERVER_BASE_URL}/agents/secrets-detection",
    STATICANALYSIS_AGENT_ID: f"{SERVER_BASE_URL}/agents/static-analysis",
    DEPSCANNER_AGENT_ID: f"{SERVER_BASE_URL}/agents/dependency-scanner",
    CVELOOKUP_AGENT_ID: f"{SERVER_BASE_URL}/agents/cve-lookup",
    SYSTEM_DESIGN_AGENT_ID: f"{SERVER_BASE_URL}/agents/system-design-reviewer",
    INCIDENT_RESPONSE_AGENT_ID: f"{SERVER_BASE_URL}/agents/incident-response-commander",
    HEALTHCARE_EXPERT_AGENT_ID: f"{SERVER_BASE_URL}/agents/healthcare-expert",
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
