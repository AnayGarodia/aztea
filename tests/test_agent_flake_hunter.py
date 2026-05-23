"""test_agent_flake_hunter.py — A1 Flake Hunter (~11 tests)."""
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

from agents import flake_hunter


_VALID_PAYLOAD = {"test_path": "tests/foo.py::test_x", "repo_root": "/tmp/x"}


def _clear_runner_env(monkeypatch):
    """Ensure the runner gate is closed unless a test explicitly opens it."""
    monkeypatch.delenv("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", raising=False)


def test_invalid_input_envelope():
    out = flake_hunter.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "flake_hunter.invalid_input")


def test_missing_test_path_rejected(monkeypatch):
    _clear_runner_env(monkeypatch)
    out = flake_hunter.run({"repo_root": "/tmp/x"})
    err = assert_error_envelope(out, "flake_hunter.invalid_input")
    assert "test_path" in err["message"]


def test_missing_repo_root_rejected(monkeypatch):
    _clear_runner_env(monkeypatch)
    out = flake_hunter.run({"test_path": "tests/foo.py"})
    err = assert_error_envelope(out, "flake_hunter.invalid_input")
    assert "repo_root" in err["message"]


def test_relative_repo_root_rejected(monkeypatch):
    _clear_runner_env(monkeypatch)
    out = flake_hunter.run({"test_path": "tests/foo.py", "repo_root": "../foo"})
    err = assert_error_envelope(out, "flake_hunter.invalid_input")
    assert "repo_root" in err["message"]


def test_trials_clamped(monkeypatch):
    """Huge trials value is accepted (clamped internally to <= 1000)."""
    _clear_runner_env(monkeypatch)
    out = flake_hunter.run({**_VALID_PAYLOAD, "trials": 9_999_999})
    # No env → still requires_configuration; the planned_trials in details
    # must be the clamped value.
    err = assert_error_envelope(out, "flake_hunter.requires_configuration")
    assert err["details"]["planned_trials"] <= 1000


def test_factors_list_optional(monkeypatch):
    """Omitting factors must not break input validation."""
    _clear_runner_env(monkeypatch)
    out = flake_hunter.run({**_VALID_PAYLOAD})
    # Without runner env, the gate still returns requires_configuration; the
    # important assertion is that we got past input validation.
    assert_error_envelope(out, "flake_hunter.requires_configuration")


def test_requires_configuration_when_runner_disabled(monkeypatch):
    _clear_runner_env(monkeypatch)
    out = flake_hunter.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out, "flake_hunter.requires_configuration")
    missing_blob = " ".join(err["details"]["missing"])
    assert "AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED" in missing_blob


def test_happy_path_reaches_reasoning_loop(monkeypatch):
    set_env_for("flake_hunter_configured", monkeypatch)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"flake_rate": 0.04, "summary": "ok"}',
    ))
    out = flake_hunter.run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out
    assert_reasoning_loop(out)


def test_reasoning_loop_two_calls(monkeypatch):
    set_env_for("flake_hunter_configured", monkeypatch)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    flake_hunter.run(_VALID_PAYLOAD)
    assert len(calls) >= 2, f"expected >= 2 LLM calls, got {len(calls)}"


def test_budget_exceeded_returns_envelope(monkeypatch):
    set_env_for("flake_hunter_configured", monkeypatch)

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=5,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = flake_hunter.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "flake_hunter.llm_error")


def test_llm_error_propagates(monkeypatch):
    set_env_for("flake_hunter_configured", monkeypatch)

    def _boom(req, *args, **kwargs):
        raise LLMError("stub", "stub-model", "upstream blew up")
    patch_llm_everywhere(monkeypatch, _boom)
    out = flake_hunter.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "flake_hunter.llm_error")
