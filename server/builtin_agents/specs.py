"""Compose built-in agent registration specs from split modules."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from server.builtin_agents.constants import (
    ARXIV_RESEARCH_AGENT_ID,
    BROWSER_AGENT_ID,
    CHANGELOG_AGENT_ID,
    CODEREVIEW_AGENT_ID,
    CVELOOKUP_AGENT_ID,
    CURATED_BUILTIN_AGENT_IDS,
    DEPRECATED_BUILTIN_AGENT_IDS,
    DEPENDENCY_AUDITOR_AGENT_ID,
    DNS_INSPECTOR_AGENT_ID,
    FINANCIAL_AGENT_ID,
    GITHUB_FETCHER_AGENT_ID,
    HN_DIGEST_AGENT_ID,
    IMAGE_GENERATOR_AGENT_ID,
    LINTER_AGENT_ID,
    LIVE_ENDPOINT_TESTER_AGENT_ID,
    MULTI_FILE_EXECUTOR_AGENT_ID,
    PACKAGE_FINDER_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID,
    QUALITY_JUDGE_AGENT_ID,
    SHELL_EXECUTOR_AGENT_ID,
    SPEC_WRITER_AGENT_ID,
    TEST_GENERATOR_AGENT_ID,
    TYPE_CHECKER_AGENT_ID,
    VIDEO_STORYBOARD_AGENT_ID,
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
    GITHUB_FETCHER_AGENT_ID: "Code",
    HN_DIGEST_AGENT_ID: "Research",
    DNS_INSPECTOR_AGENT_ID: "Security",
    TEST_GENERATOR_AGENT_ID: "Code",
    SPEC_WRITER_AGENT_ID: "Code",
    CHANGELOG_AGENT_ID: "Data",
    PACKAGE_FINDER_AGENT_ID: "Data",
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
    GITHUB_FETCHER_AGENT_ID: True,
    HN_DIGEST_AGENT_ID: True,
    DNS_INSPECTOR_AGENT_ID: False,
    LINTER_AGENT_ID: False,
    TEST_GENERATOR_AGENT_ID: True,
    SPEC_WRITER_AGENT_ID: True,
    CHANGELOG_AGENT_ID: True,
    PACKAGE_FINDER_AGENT_ID: True,
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
    GITHUB_FETCHER_AGENT_ID: ["httpx", "llm provider optional for synthesis"],
    HN_DIGEST_AGENT_ID: ["httpx", "llm provider optional for synthesis"],
    DNS_INSPECTOR_AGENT_ID: ["socket", "ssl"],
    LINTER_AGENT_ID: ["ruff", "node/eslint optional for js/ts"],
    TEST_GENERATOR_AGENT_ID: ["llm provider", "python3"],
    SPEC_WRITER_AGENT_ID: ["llm provider"],
    CHANGELOG_AGENT_ID: ["requests", "llm provider optional for synthesis"],
    PACKAGE_FINDER_AGENT_ID: ["requests", "llm provider"],
    DEPENDENCY_AUDITOR_AGENT_ID: ["requests"],
    MULTI_FILE_EXECUTOR_AGENT_ID: ["python3", "pip"],
    SHELL_EXECUTOR_AGENT_ID: ["allowlisted local binaries"],
    TYPE_CHECKER_AGENT_ID: ["mypy"],
    LIVE_ENDPOINT_TESTER_AGENT_ID: ["requests"],
    BROWSER_AGENT_ID: ["playwright", "chromium"],
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
    if agent_id in CURATED_BUILTIN_AGENT_IDS and agent_id not in DEPRECATED_BUILTIN_AGENT_IDS:
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
            "is_featured": bool(spec.get("is_featured", agent_id not in DEPRECATED_BUILTIN_AGENT_IDS)),
            "runtime_requirements": list(runtime_requirements),
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
    specs = list(_all_builtin_specs())
    result = []
    for spec in specs:
        agent_id = spec.get("agent_id")
        if agent_id in CURATED_BUILTIN_AGENT_IDS:
            result.append(spec)
        elif agent_id in DEPRECATED_BUILTIN_AGENT_IDS:
            # Register deprecated agents normally so existing callers can still
            # invoke them, but mark them deprecated so the registry list can
            # filter them from public discovery.
            result.append({**spec, "deprecated": True})
    return result


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
        "deprecated": bool(spec.get("deprecated", False)),
    }
