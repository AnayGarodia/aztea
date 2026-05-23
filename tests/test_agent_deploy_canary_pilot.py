"""test_agent_deploy_canary_pilot.py — A3 Deploy Canary Pilot (~10 tests)."""
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

from agents import deploy_canary_pilot


_VALID_PAYLOAD = {"deploy_cmd": "deploy", "slo_thresholds": {"p95_ms": 500}}


def _clear_deploy_env(monkeypatch):
    monkeypatch.delenv("AZTEA_DEPLOY_API_TOKEN", raising=False)
    monkeypatch.delenv("AZTEA_METRICS_API_URL", raising=False)


def test_invalid_input_envelope():
    out = deploy_canary_pilot.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "deploy_canary_pilot.invalid_input")


def test_missing_deploy_cmd_rejected(monkeypatch):
    _clear_deploy_env(monkeypatch)
    out = deploy_canary_pilot.run({"slo_thresholds": {"p95_ms": 500}})
    err = assert_error_envelope(out, "deploy_canary_pilot.invalid_input")
    assert "deploy_cmd" in err["message"]


@pytest.mark.parametrize("bad_slo", [["a", "b"], "string", None])
def test_slo_thresholds_must_be_dict(monkeypatch, bad_slo):
    _clear_deploy_env(monkeypatch)
    out = deploy_canary_pilot.run({"deploy_cmd": "deploy", "slo_thresholds": bad_slo})
    assert_error_envelope(out, "deploy_canary_pilot.invalid_input")


def test_empty_slo_thresholds_rejected(monkeypatch):
    _clear_deploy_env(monkeypatch)
    out = deploy_canary_pilot.run({"deploy_cmd": "deploy", "slo_thresholds": {}})
    assert_error_envelope(out, "deploy_canary_pilot.invalid_input")


def test_watch_seconds_clamped_to_14400(monkeypatch):
    """A huge watch_seconds is accepted; the value is clamped to <= 14400."""
    _clear_deploy_env(monkeypatch)
    out = deploy_canary_pilot.run({**_VALID_PAYLOAD, "watch_seconds": 999_999})
    err = assert_error_envelope(out,
                                "deploy_canary_pilot.requires_configuration")
    assert err["details"]["watch_seconds"] <= 14400


def test_requires_configuration_when_both_env_missing(monkeypatch):
    _clear_deploy_env(monkeypatch)
    out = deploy_canary_pilot.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out,
                                "deploy_canary_pilot.requires_configuration")
    missing = err["details"]["missing"]
    assert "AZTEA_DEPLOY_API_TOKEN" in missing
    assert "AZTEA_METRICS_API_URL" in missing


def test_requires_configuration_when_only_one_env_set(monkeypatch):
    _clear_deploy_env(monkeypatch)
    monkeypatch.setenv("AZTEA_DEPLOY_API_TOKEN", "tok-fake")
    # AZTEA_METRICS_API_URL still missing.
    out = deploy_canary_pilot.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out,
                                "deploy_canary_pilot.requires_configuration")
    missing = err["details"]["missing"]
    assert "AZTEA_METRICS_API_URL" in missing
    assert "AZTEA_DEPLOY_API_TOKEN" not in missing


def test_happy_path_reaches_reasoning_loop(monkeypatch):
    set_env_for("deploy_canary_configured", monkeypatch)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict": "promoted", "rationale": "ok"}',
    ))
    out = deploy_canary_pilot.run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out
    assert_reasoning_loop(out)


def test_reasoning_loop_two_calls(monkeypatch):
    set_env_for("deploy_canary_configured", monkeypatch)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    deploy_canary_pilot.run(_VALID_PAYLOAD)
    assert len(calls) >= 2, f"expected >= 2 LLM calls, got {len(calls)}"


def test_budget_exceeded_returns_envelope(monkeypatch):
    set_env_for("deploy_canary_configured", monkeypatch)

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=5,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = deploy_canary_pilot.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "deploy_canary_pilot.llm_error")
