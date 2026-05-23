"""test_agent_fuzz_and_find.py — B6 Fuzz-and-Find (~11 tests)."""
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

from agents import fuzz_and_find


_VALID_PAYLOAD = {
    "function_source": "def f(x): return x*2",
    "property_spec": "monotonic",
}


def _clear_runner_env(monkeypatch):
    """Ensure the runner gate is closed unless a test explicitly opens it."""
    monkeypatch.delenv("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", raising=False)


def test_invalid_input_envelope():
    out = fuzz_and_find.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "fuzz_and_find.invalid_input")


def test_missing_function_source_rejected(monkeypatch):
    _clear_runner_env(monkeypatch)
    out = fuzz_and_find.run({"property_spec": "monotonic"})
    err = assert_error_envelope(out, "fuzz_and_find.invalid_input")
    assert "function_source" in err["message"]


def test_missing_property_spec_rejected(monkeypatch):
    _clear_runner_env(monkeypatch)
    out = fuzz_and_find.run({"function_source": "def f(x): return x"})
    err = assert_error_envelope(out, "fuzz_and_find.invalid_input")
    assert "property_spec" in err["message"]


def test_iterations_clamped_to_million(monkeypatch):
    """iterations=10**9 must clamp to <= 1_000_000."""
    _clear_runner_env(monkeypatch)
    out = fuzz_and_find.run({**_VALID_PAYLOAD, "iterations": 10**9})
    # Without runner env, returns requires_configuration with the clamped value
    # surfaced in details.
    err = assert_error_envelope(out, "fuzz_and_find.requires_configuration")
    assert err["details"]["iterations"] <= 1_000_000


def test_property_spec_empty_rejected(monkeypatch):
    _clear_runner_env(monkeypatch)
    out = fuzz_and_find.run({"function_source": "def f(x): return x",
                             "property_spec": "   "})
    err = assert_error_envelope(out, "fuzz_and_find.invalid_input")
    assert "property_spec" in err["message"]


def test_requires_configuration_when_runner_disabled(monkeypatch):
    _clear_runner_env(monkeypatch)
    out = fuzz_and_find.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out, "fuzz_and_find.requires_configuration")
    missing_blob = " ".join(err["details"]["missing"])
    assert "AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED" in missing_blob


def test_happy_path_reaches_reasoning_loop(monkeypatch):
    set_env_for("fuzz_and_find_configured", monkeypatch)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"plan":"p","synthesis":"s"}',
    ))
    out = fuzz_and_find.run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out
    assert_reasoning_loop(out)


def test_reasoning_loop_two_calls(monkeypatch):
    set_env_for("fuzz_and_find_configured", monkeypatch)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    fuzz_and_find.run(_VALID_PAYLOAD)
    assert len(calls) >= 2, f"expected >= 2 LLM calls, got {len(calls)}"


def test_budget_exceeded_returns_envelope(monkeypatch):
    set_env_for("fuzz_and_find_configured", monkeypatch)

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=10,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = fuzz_and_find.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "fuzz_and_find.llm_error")


def test_llm_error_propagates(monkeypatch):
    set_env_for("fuzz_and_find_configured", monkeypatch)

    def _boom(req, *args, **kwargs):
        raise LLMError("stub", "stub-model", "upstream down")
    patch_llm_everywhere(monkeypatch, _boom)
    out = fuzz_and_find.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "fuzz_and_find.llm_error")


def test_plan_includes_iteration_count(monkeypatch):
    """Happy-path: the planned iteration count must appear in the plan
    LLM call's user prompt so the model can shape its strategy."""
    set_env_for("fuzz_and_find_configured", monkeypatch)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    fuzz_and_find.run({**_VALID_PAYLOAD, "iterations": 5000})
    plan_user = next(m.content for m in calls[0].messages if m.role == "user")
    assert "5000" in plan_user, (
        f"expected iteration count 5000 in plan user message; got: "
        f"{plan_user[:300]!r}"
    )
