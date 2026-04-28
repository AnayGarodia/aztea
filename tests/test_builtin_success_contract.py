from __future__ import annotations

import server.application as server


def _refresh_builtin_spec_cache() -> None:
    server._builtin_specs._all_builtin_specs.cache_clear()
    server._builtin_specs.builtin_spec_by_id.cache_clear()


def test_builtin_dispatch_adds_default_success_contract(monkeypatch):
    _refresh_builtin_spec_cache()
    monkeypatch.setattr(server.agent_db_sandbox, "run", lambda payload: {"engine": "sqlite", "results": []})
    result = server._execute_builtin_agent(server._DB_SANDBOX_AGENT_ID, {})
    assert result["billing_units_actual"] == 1
    assert result["degraded_mode"] is False
    assert result["llm_used"] is False
    assert result["agent_contract_version"] == "builtin-v2"


def test_builtin_dispatch_infers_llm_usage_for_llm_backed_builtin(monkeypatch):
    _refresh_builtin_spec_cache()
    monkeypatch.setattr(
        server.agent_spec_writer,
        "run",
        lambda payload: {"title": "Spec", "format": "rfc", "sections": [], "open_questions": [], "out_of_scope": [], "estimated_complexity": "M", "full_text": "Spec"},
    )
    result = server._execute_builtin_agent(server._SPEC_WRITER_AGENT_ID, {})
    assert result["billing_units_actual"] == 1
    assert result["degraded_mode"] is False
    assert result["llm_used"] is True
    assert result["agent_contract_version"] == "builtin-v2"


def test_builtin_dispatch_preserves_explicit_degraded_metadata(monkeypatch):
    _refresh_builtin_spec_cache()
    monkeypatch.setattr(
        server.agent_web_researcher,
        "run",
        lambda payload: {
            "summary": "Fetched content without synthesis.",
            "billing_units_actual": 1,
            "llm_used": False,
            "degraded_mode": True,
        },
    )
    result = server._execute_builtin_agent(server._WEB_RESEARCHER_AGENT_ID, {})
    assert result["billing_units_actual"] == 1
    assert result["degraded_mode"] is True
    assert result["llm_used"] is False


def test_builtin_dispatch_leaves_structured_errors_untouched(monkeypatch):
    _refresh_builtin_spec_cache()
    monkeypatch.setattr(
        server.agent_package_finder,
        "run",
        lambda payload: {"error": {"code": "package_finder.missing_task", "message": "task is required."}},
    )
    result = server._execute_builtin_agent(server._PACKAGE_FINDER_AGENT_ID, {})
    assert result == {"error": {"code": "package_finder.missing_task", "message": "task is required."}}
