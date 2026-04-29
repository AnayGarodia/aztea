"""Compose built-in agent registration specs from split modules."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from server.builtin_agents.constants import (
    AI_RED_TEAMER_AGENT_ID,
    ARXIV_RESEARCH_AGENT_ID,
    BROWSER_AGENT_ID,
    CODEREVIEW_AGENT_ID,
    CVELOOKUP_AGENT_ID,
    CURATED_BUILTIN_AGENT_IDS,
    DB_SANDBOX_AGENT_ID,
    DEPENDENCY_AUDITOR_AGENT_ID,
    DNS_INSPECTOR_AGENT_ID,
    FINANCIAL_AGENT_ID,
    HN_DIGEST_AGENT_ID,
    IMAGE_GENERATOR_AGENT_ID,
    LINTER_AGENT_ID,
    LIVE_ENDPOINT_TESTER_AGENT_ID,
    MULTI_FILE_EXECUTOR_AGENT_ID,
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID,
    QUALITY_JUDGE_AGENT_ID,
    SEMANTIC_CODEBASE_SEARCH_AGENT_ID,
    SHELL_EXECUTOR_AGENT_ID,
    TYPE_CHECKER_AGENT_ID,
    VIDEO_STORYBOARD_AGENT_ID,
    VISUAL_REGRESSION_AGENT_ID,
    WEB_RESEARCHER_AGENT_ID,
    WIKI_AGENT_ID,
)
from server.builtin_agents.specs_part1 import load_builtin_specs_part1
from server.builtin_agents.specs_part2 import load_builtin_specs_part2
from server.builtin_agents.specs_part3 import load_builtin_specs_part3
from server.builtin_agents.specs_part4 import load_builtin_specs_part4

_DEFAULT_CATEGORY_BY_AGENT_ID = {
    FINANCIAL_AGENT_ID: "Finance",
    CODEREVIEW_AGENT_ID: "Code",
    WIKI_AGENT_ID: "Research",
    QUALITY_JUDGE_AGENT_ID: "Internal",
    CVELOOKUP_AGENT_ID: "Security",
    IMAGE_GENERATOR_AGENT_ID: "Media",
    VIDEO_STORYBOARD_AGENT_ID: "Media",
    ARXIV_RESEARCH_AGENT_ID: "Research",
    PYTHON_EXECUTOR_AGENT_ID: "Code Execution",
    WEB_RESEARCHER_AGENT_ID: "Web",
    HN_DIGEST_AGENT_ID: "Research",
    DNS_INSPECTOR_AGENT_ID: "Security",
    DEPENDENCY_AUDITOR_AGENT_ID: "Code",
    MULTI_FILE_EXECUTOR_AGENT_ID: "Code Execution",
    SHELL_EXECUTOR_AGENT_ID: "Code Execution",
    TYPE_CHECKER_AGENT_ID: "Code",
    LIVE_ENDPOINT_TESTER_AGENT_ID: "QA",
    BROWSER_AGENT_ID: "Web",
}

_DEFAULT_CACHEABLE_BY_AGENT_ID = {
    FINANCIAL_AGENT_ID: True,
    CODEREVIEW_AGENT_ID: True,
    WIKI_AGENT_ID: True,
    QUALITY_JUDGE_AGENT_ID: False,
    CVELOOKUP_AGENT_ID: True,
    IMAGE_GENERATOR_AGENT_ID: False,
    VIDEO_STORYBOARD_AGENT_ID: False,
    ARXIV_RESEARCH_AGENT_ID: True,
    PYTHON_EXECUTOR_AGENT_ID: False,
    WEB_RESEARCHER_AGENT_ID: True,
    HN_DIGEST_AGENT_ID: True,
    DNS_INSPECTOR_AGENT_ID: False,
    LINTER_AGENT_ID: False,
    DEPENDENCY_AUDITOR_AGENT_ID: True,
    MULTI_FILE_EXECUTOR_AGENT_ID: False,
    SHELL_EXECUTOR_AGENT_ID: False,
    TYPE_CHECKER_AGENT_ID: False,
}

_DEFAULT_RUNTIME_REQUIREMENTS_BY_AGENT_ID = {
    CVELOOKUP_AGENT_ID: ["requests"],
    IMAGE_GENERATOR_AGENT_ID: ["configured media backend"],
    VIDEO_STORYBOARD_AGENT_ID: ["configured media backend"],
    PYTHON_EXECUTOR_AGENT_ID: ["python3"],
    WEB_RESEARCHER_AGENT_ID: ["requests", "llm provider optional for synthesis"],
    HN_DIGEST_AGENT_ID: ["httpx", "llm provider optional for synthesis"],
    DNS_INSPECTOR_AGENT_ID: ["socket", "ssl"],
    LINTER_AGENT_ID: ["ruff", "node/eslint optional for js/ts"],
    DEPENDENCY_AUDITOR_AGENT_ID: ["requests"],
    MULTI_FILE_EXECUTOR_AGENT_ID: ["python3", "pip"],
    SHELL_EXECUTOR_AGENT_ID: ["allowlisted local binaries"],
    TYPE_CHECKER_AGENT_ID: ["mypy"],
    LIVE_ENDPOINT_TESTER_AGENT_ID: ["requests"],
    BROWSER_AGENT_ID: ["playwright", "chromium"],
}

_DEFAULT_TOOLING_KIND_BY_AGENT_ID = {
    FINANCIAL_AGENT_ID: "live_data_plus_llm",
    CODEREVIEW_AGENT_ID: "llm_structured_analysis",
    WIKI_AGENT_ID: "live_fetch_plus_llm",
    QUALITY_JUDGE_AGENT_ID: "internal_judge",
    CVELOOKUP_AGENT_ID: "live_api",
    IMAGE_GENERATOR_AGENT_ID: "model_backend",
    VIDEO_STORYBOARD_AGENT_ID: "model_backend",
    ARXIV_RESEARCH_AGENT_ID: "live_api_plus_llm",
    PYTHON_EXECUTOR_AGENT_ID: "sandbox_execution",
    WEB_RESEARCHER_AGENT_ID: "live_fetch_plus_llm",
    HN_DIGEST_AGENT_ID: "live_fetch_plus_llm",
    DNS_INSPECTOR_AGENT_ID: "live_network_checks",
    LINTER_AGENT_ID: "tool_execution",
    DEPENDENCY_AUDITOR_AGENT_ID: "live_api_analysis",
    MULTI_FILE_EXECUTOR_AGENT_ID: "sandbox_execution",
    SHELL_EXECUTOR_AGENT_ID: "sandbox_execution",
    TYPE_CHECKER_AGENT_ID: "tool_execution",
    LIVE_ENDPOINT_TESTER_AGENT_ID: "live_network_checks",
    BROWSER_AGENT_ID: "browser_automation",
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID: "sandbox_execution",
    SEMANTIC_CODEBASE_SEARCH_AGENT_ID: "hybrid_search",
    AI_RED_TEAMER_AGENT_ID: "agent_adversarial_testing",
}

_DEFAULT_STABILITY_TIER_BY_AGENT_ID = {
    FINANCIAL_AGENT_ID: "stable",
    CODEREVIEW_AGENT_ID: "stable",
    WIKI_AGENT_ID: "stable",
    QUALITY_JUDGE_AGENT_ID: "internal",
    CVELOOKUP_AGENT_ID: "stable",
    IMAGE_GENERATOR_AGENT_ID: "experimental",
    VIDEO_STORYBOARD_AGENT_ID: "experimental",
    ARXIV_RESEARCH_AGENT_ID: "stable",
    PYTHON_EXECUTOR_AGENT_ID: "stable",
    WEB_RESEARCHER_AGENT_ID: "stable",
    HN_DIGEST_AGENT_ID: "stable",
    DNS_INSPECTOR_AGENT_ID: "stable",
    LINTER_AGENT_ID: "stable",
    DEPENDENCY_AUDITOR_AGENT_ID: "stable",
    MULTI_FILE_EXECUTOR_AGENT_ID: "stable",
    SHELL_EXECUTOR_AGENT_ID: "stable",
    TYPE_CHECKER_AGENT_ID: "stable",
    LIVE_ENDPOINT_TESTER_AGENT_ID: "stable",
    BROWSER_AGENT_ID: "stable",
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID: "beta",
    SEMANTIC_CODEBASE_SEARCH_AGENT_ID: "beta",
    AI_RED_TEAMER_AGENT_ID: "beta",
}

_DEFAULT_CODEX_RECOMMENDED_BY_AGENT_ID = {
    FINANCIAL_AGENT_ID: False,
    CODEREVIEW_AGENT_ID: True,
    WIKI_AGENT_ID: False,
    QUALITY_JUDGE_AGENT_ID: False,
    CVELOOKUP_AGENT_ID: True,
    IMAGE_GENERATOR_AGENT_ID: False,
    VIDEO_STORYBOARD_AGENT_ID: False,
    ARXIV_RESEARCH_AGENT_ID: False,
    PYTHON_EXECUTOR_AGENT_ID: True,
    WEB_RESEARCHER_AGENT_ID: True,
    HN_DIGEST_AGENT_ID: False,
    DNS_INSPECTOR_AGENT_ID: True,
    LINTER_AGENT_ID: True,
    DEPENDENCY_AUDITOR_AGENT_ID: True,
    MULTI_FILE_EXECUTOR_AGENT_ID: True,
    SHELL_EXECUTOR_AGENT_ID: True,
    TYPE_CHECKER_AGENT_ID: True,
    LIVE_ENDPOINT_TESTER_AGENT_ID: True,
    BROWSER_AGENT_ID: True,
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID: True,
    SEMANTIC_CODEBASE_SEARCH_AGENT_ID: True,
    AI_RED_TEAMER_AGENT_ID: False,
}

_DEFAULT_SHORT_USE_CASES_BY_AGENT_ID = {
    CODEREVIEW_AGENT_ID: ["review a snippet", "review a diff", "security-focused review"],
    CVELOOKUP_AGENT_ID: ["look up a CVE ID", "check affected package versions"],
    ARXIV_RESEARCH_AGENT_ID: ["find recent papers", "scan a research topic"],
    PYTHON_EXECUTOR_AGENT_ID: ["run a snippet", "verify runtime behavior"],
    WEB_RESEARCHER_AGENT_ID: ["fetch live docs", "compare a few web sources"],
    DNS_INSPECTOR_AGENT_ID: ["check DNS", "check SSL expiry", "inspect headers"],
    DEPENDENCY_AUDITOR_AGENT_ID: ["audit requirements.txt", "audit package.json"],
    MULTI_FILE_EXECUTOR_AGENT_ID: ["run a small project", "verify imports and dependencies"],
    LINTER_AGENT_ID: ["lint Python", "lint JS/TS"],
    SHELL_EXECUTOR_AGENT_ID: ["run tests", "inspect git state", "verify toolchain"],
    TYPE_CHECKER_AGENT_ID: ["run mypy", "run tsc"],
    DB_SANDBOX_AGENT_ID: ["test SQL", "inspect query plans"],
    VISUAL_REGRESSION_AGENT_ID: ["compare screenshots", "highlight changed pixels"],
    LIVE_ENDPOINT_TESTER_AGENT_ID: ["probe latency", "load-test a health endpoint"],
    BROWSER_AGENT_ID: ["render a page", "capture screenshot of SPA"],
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID: ["run JS/TS", "run Go", "run Rust"],
    SEMANTIC_CODEBASE_SEARCH_AGENT_ID: ["find implementation", "trace a feature across files"],
}


def _normalize_builtin_spec(spec: dict[str, Any]) -> dict[str, Any]:
    agent_id = str(spec.get("agent_id") or "").strip()
    if not agent_id:
        raise ValueError("Built-in spec is missing agent_id.")
    endpoint_url = str(spec.get("endpoint_url") or "").strip()
    if not endpoint_url.startswith("internal://"):
        raise ValueError(f"Built-in spec {agent_id} must use an internal:// endpoint.")
    output_examples = spec.get("output_examples")
    if not isinstance(output_examples, list) or not output_examples:
        raise ValueError(f"Built-in spec {agent_id} must include at least one output example.")
    if agent_id in CURATED_BUILTIN_AGENT_IDS:
        category = str(spec.get("category") or _DEFAULT_CATEGORY_BY_AGENT_ID.get(agent_id) or "").strip()
        if not category:
            raise ValueError(f"Built-in spec {agent_id} is missing category metadata.")
        cacheable = spec.get("cacheable")
        if cacheable is None:
            if agent_id not in _DEFAULT_CACHEABLE_BY_AGENT_ID:
                raise ValueError(f"Built-in spec {agent_id} is missing cacheable metadata.")
            cacheable = _DEFAULT_CACHEABLE_BY_AGENT_ID[agent_id]
        runtime_requirements = spec.get("runtime_requirements")
        if runtime_requirements is None:
            runtime_requirements = _DEFAULT_RUNTIME_REQUIREMENTS_BY_AGENT_ID.get(agent_id, [])
        return {
            **spec,
            "category": category,
            "cacheable": bool(cacheable),
            "is_featured": bool(spec.get("is_featured", True)),
            "runtime_requirements": list(runtime_requirements),
            "tooling_kind": str(spec.get("tooling_kind") or _DEFAULT_TOOLING_KIND_BY_AGENT_ID.get(agent_id) or "tool_execution"),
            "stability_tier": str(spec.get("stability_tier") or _DEFAULT_STABILITY_TIER_BY_AGENT_ID.get(agent_id) or "stable"),
            "codex_recommended": bool(spec.get("codex_recommended", _DEFAULT_CODEX_RECOMMENDED_BY_AGENT_ID.get(agent_id, False))),
            "short_use_cases": list(spec.get("short_use_cases") or _DEFAULT_SHORT_USE_CASES_BY_AGENT_ID.get(agent_id, [])),
        }
    return dict(spec)


@lru_cache(maxsize=1)
def _all_builtin_specs() -> tuple[dict[str, Any], ...]:
    specs = load_builtin_specs_part1()
    specs.extend(load_builtin_specs_part2())
    specs.extend(load_builtin_specs_part3())
    specs.extend(load_builtin_specs_part4())
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
    return [spec for spec in _all_builtin_specs() if spec.get("agent_id") in CURATED_BUILTIN_AGENT_IDS]


@lru_cache(maxsize=1)
def builtin_spec_by_id() -> dict[str, dict[str, Any]]:
    return {
        str(spec["agent_id"]): dict(spec)
        for spec in builtin_agent_specs()
    }


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
