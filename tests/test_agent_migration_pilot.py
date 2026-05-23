"""test_agent_migration_pilot.py — A4 Migration Pilot (~9 tests)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core.llm.errors import BudgetExceededError, LLMError
from tests.agent_helpers import (
    _capture_llm_calls,
    _make_response,
    _stub_llm_factory,
    assert_error_envelope,
    assert_reasoning_loop,
    patch_llm_everywhere,
    set_env_for,
)

from agents import migration_pilot


_VALID_PAYLOAD = {"target_sql": "ALTER TABLE foo ADD COLUMN x INTEGER"}


def _clear_replica_env(monkeypatch):
    monkeypatch.delenv("AZTEA_MIGRATION_REPLICA_DSN", raising=False)


def test_invalid_input_envelope():
    out = migration_pilot.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "migration_pilot.invalid_input")


def test_missing_target_sql_rejected(monkeypatch):
    _clear_replica_env(monkeypatch)
    out = migration_pilot.run({})
    err = assert_error_envelope(out, "migration_pilot.invalid_input")
    assert "target_sql" in err["message"]


def test_drop_requires_explicit_allow(monkeypatch):
    """SQL containing DROP without allow_drops=true is rejected."""
    _clear_replica_env(monkeypatch)
    out = migration_pilot.run({"target_sql": "DROP TABLE foo"})
    err = assert_error_envelope(out, "migration_pilot.invalid_input")
    assert "DROP" in err["message"] or "allow_drops" in err["message"]


def test_drop_with_allow_drops_passes_validation(monkeypatch):
    """DROP with allow_drops=true passes input validation (then hits config gate)."""
    _clear_replica_env(monkeypatch)
    out = migration_pilot.run({
        "target_sql": "DROP TABLE foo", "allow_drops": True,
    })
    # No replica env → requires_configuration, meaning we passed validation.
    assert_error_envelope(out, "migration_pilot.requires_configuration")


def test_lock_threshold_clamped(monkeypatch):
    """A huge lock_threshold_ms is accepted (clamped internally to <= 600_000)."""
    _clear_replica_env(monkeypatch)
    out = migration_pilot.run({**_VALID_PAYLOAD, "lock_threshold_ms": 999_999_999})
    # No replica DSN → requires_configuration; the call itself must succeed.
    assert_error_envelope(out, "migration_pilot.requires_configuration")


def test_requires_configuration_without_replica_dsn(monkeypatch):
    _clear_replica_env(monkeypatch)
    out = migration_pilot.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out, "migration_pilot.requires_configuration")
    assert "AZTEA_MIGRATION_REPLICA_DSN" in err["details"]["missing"]


def test_happy_path_reaches_reasoning_loop(monkeypatch):
    set_env_for("migration_pilot_configured", monkeypatch)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"strategy": "concurrent", "stages": []}',
    ))
    out = migration_pilot.run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out
    assert_reasoning_loop(out)


def test_reasoning_loop_two_calls(monkeypatch):
    set_env_for("migration_pilot_configured", monkeypatch)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    migration_pilot.run(_VALID_PAYLOAD)
    assert len(calls) >= 2, f"expected >= 2 LLM calls, got {len(calls)}"


def test_budget_exceeded_returns_envelope(monkeypatch):
    set_env_for("migration_pilot_configured", monkeypatch)

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=5,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = migration_pilot.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "migration_pilot.llm_error")
