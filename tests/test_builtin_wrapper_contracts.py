from __future__ import annotations

from agents import changelog_agent
from agents import package_finder
from agents import pr_reviewer
from agents import spec_writer
from agents import test_generator


def test_package_finder_returns_structured_error_for_missing_task():
    result = package_finder.run({})
    assert result["error"]["code"] == "package_finder.missing_task"


def test_package_finder_returns_structured_error_for_invalid_count():
    result = package_finder.run({"task": "http client", "count": "abc"})
    assert result["error"]["code"] == "package_finder.invalid_count"


def test_changelog_agent_returns_structured_error_for_missing_package():
    result = changelog_agent.run({})
    assert result["error"]["code"] == "changelog_agent.missing_package"


def test_changelog_agent_degrades_cleanly_without_llm(monkeypatch):
    monkeypatch.setattr(changelog_agent, "_fetch_pypi_info", lambda package: {
        "info": {
            "version": "2.0.0",
            "description": "Long changelog body for release history",
            "home_page": "",
            "project_url": "",
            "package_url": "",
            "project_urls": {},
        },
        "releases": {},
    })
    monkeypatch.setattr(changelog_agent, "run_with_fallback", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no llm")))
    result = changelog_agent.run({"package": "requests", "ecosystem": "pypi"})
    assert result["billing_units_actual"] == 1
    assert result["llm_used"] is False
    assert result["degraded_mode"] is True
    assert "LLM synthesis is unavailable" in result["summary"]


def test_test_generator_returns_structured_error_without_llm(monkeypatch):
    monkeypatch.setattr(test_generator, "run_with_fallback", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no llm")))
    result = test_generator.run({"code": "def add(a, b): return a + b"})
    assert result["error"]["code"] == "test_generator.tool_unavailable"


def test_spec_writer_returns_structured_error_without_llm(monkeypatch):
    monkeypatch.setattr(spec_writer, "run_with_fallback", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no llm")))
    result = spec_writer.run({"requirements": "Build a webhook delivery retry policy."})
    assert result["error"]["code"] == "spec_writer.tool_unavailable"


def test_pr_reviewer_returns_structured_error_for_missing_input():
    result = pr_reviewer.run({})
    assert result["error"]["code"] == "pr_reviewer.missing_input"


def test_pr_reviewer_returns_structured_error_for_invalid_pr_url():
    result = pr_reviewer.run({"pr_url": "https://example.com/not-a-github-pr"})
    assert result["error"]["code"] == "pr_reviewer.invalid_pr_url"
