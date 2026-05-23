"""test_agent_schema_migration_planner.py — D19 Schema Migration Planner (~10 tests)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core.llm.errors import BudgetExceededError, LLMError
from tests.agent_helpers import (
    _capture_llm_calls, _stub_llm_factory,
    assert_error_envelope, patch_llm_everywhere, set_env_for,
)

from agents import schema_migration_planner


_BASE = {
    "current_schema": "CREATE TABLE x (id INT, name TEXT)",
    "target_schema": "CREATE TABLE x (id INT)",
}


def test_invalid_input_envelope():
    out = schema_migration_planner.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "schema_migration_planner.invalid_input")


def test_missing_current_schema_rejected():
    out = schema_migration_planner.run({"target_schema": "CREATE TABLE x (id INT)"})
    err = assert_error_envelope(out, "schema_migration_planner.invalid_input")
    assert "current_schema" in err["message"] or "target_schema" in err["message"]


def test_missing_target_schema_rejected():
    out = schema_migration_planner.run({"current_schema": "CREATE TABLE x (id INT)"})
    err = assert_error_envelope(out, "schema_migration_planner.invalid_input")
    assert "current_schema" in err["message"] or "target_schema" in err["message"]


def test_both_schemas_required():
    out = schema_migration_planner.run({})
    err = assert_error_envelope(out, "schema_migration_planner.invalid_input")
    assert "required" in err["message"]


def test_query_log_path_required():
    out = schema_migration_planner.run({**_BASE})
    err = assert_error_envelope(
        out, "schema_migration_planner.requires_configuration",
    )
    missing_blob = " ".join(err["details"]["missing"])
    assert "query_log_path" in missing_blob


def test_query_log_must_exist(tmp_path):
    out = schema_migration_planner.run({
        **_BASE, "query_log_path": str(tmp_path / "nope.csv"),
    })
    err = assert_error_envelope(
        out, "schema_migration_planner.requires_configuration",
    )
    missing_blob = " ".join(err["details"]["missing"])
    assert "query_log_path" in missing_blob


def test_query_log_must_be_file_not_dir(tmp_path):
    """Passing a directory path → os.path.isfile returns False → requires_configuration."""
    out = schema_migration_planner.run({
        **_BASE, "query_log_path": str(tmp_path),  # directory, not a file
    })
    assert_error_envelope(
        out, "schema_migration_planner.requires_configuration",
    )


def test_happy_path_with_log_file(monkeypatch, tmp_path):
    log = tmp_path / "log.csv"
    log.write_text("query,count\nSELECT 1,1\n")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"stages":[]}',
    ))
    out = schema_migration_planner.run({
        **_BASE, "query_log_path": str(log),
    })
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out


def test_reasoning_loop_two_calls(monkeypatch, tmp_path):
    log = tmp_path / "log.csv"
    log.write_text("query,count\nSELECT 1,1\n")
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    schema_migration_planner.run({
        **_BASE, "query_log_path": str(log),
    })
    assert len(calls) >= 2, f"expected >= 2 LLM calls, got {len(calls)}"


def test_budget_exceeded_returns_envelope(monkeypatch, tmp_path):
    log = tmp_path / "log.csv"
    log.write_text("query,count\nSELECT 1,1\n")

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=5,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = schema_migration_planner.run({
        **_BASE, "query_log_path": str(log),
    })
    assert_error_envelope(out, "schema_migration_planner.llm_error")
