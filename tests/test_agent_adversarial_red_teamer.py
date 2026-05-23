"""test_agent_adversarial_red_teamer.py — E22 Adversarial Red Teamer (~12 tests)."""
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

from agents import adversarial_red_teamer


_VALID_PAYLOAD = {
    "target_url": "https://target.example.com/api",
    "goal": "find auth bypass",
    "consent_token": "consent-fake",
}


def _clear_env(monkeypatch):
    monkeypatch.delenv("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", raising=False)
    monkeypatch.delenv("AZTEA_REDTEAM_CONSENT_SIGNING_KEY", raising=False)


def test_invalid_input_envelope():
    out = adversarial_red_teamer.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "adversarial_red_teamer.invalid_input")


def test_missing_target_url_rejected(monkeypatch):
    _clear_env(monkeypatch)
    out = adversarial_red_teamer.run({"goal": "x", "consent_token": "c"})
    err = assert_error_envelope(out, "adversarial_red_teamer.invalid_input")
    assert "target_url" in err["message"]


def test_non_http_target_url_rejected(monkeypatch):
    _clear_env(monkeypatch)
    out = adversarial_red_teamer.run({
        "target_url": "file://attack.target",
        "goal": "x", "consent_token": "c",
    })
    err = assert_error_envelope(out, "adversarial_red_teamer.invalid_input")
    assert "HTTP" in err["message"] or "http" in err["message"]


def test_missing_goal_rejected(monkeypatch):
    _clear_env(monkeypatch)
    out = adversarial_red_teamer.run({
        "target_url": "https://target.example.com", "consent_token": "c",
    })
    err = assert_error_envelope(out, "adversarial_red_teamer.invalid_input")
    assert "goal" in err["message"]


def test_missing_consent_token_returns_authorization_required(monkeypatch):
    """Missing consent token → authorization_required (NOT requires_configuration)."""
    _clear_env(monkeypatch)
    out = adversarial_red_teamer.run({
        "target_url": "https://target.example.com",
        "goal": "find auth bypass",
    })
    assert_error_envelope(
        out, "adversarial_red_teamer.authorization_required",
    )


def test_empty_consent_token_returns_authorization_required(monkeypatch):
    """Empty string consent token is treated as missing."""
    _clear_env(monkeypatch)
    out = adversarial_red_teamer.run({
        "target_url": "https://target.example.com",
        "goal": "find auth bypass",
        "consent_token": "",
    })
    assert_error_envelope(
        out, "adversarial_red_teamer.authorization_required",
    )


def test_requires_configuration_when_runner_disabled_with_consent(monkeypatch):
    """Consent supplied but runner env missing → requires_configuration."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("AZTEA_REDTEAM_CONSENT_SIGNING_KEY", "fake-key")
    out = adversarial_red_teamer.run(_VALID_PAYLOAD)
    err = assert_error_envelope(
        out, "adversarial_red_teamer.requires_configuration",
    )
    missing_blob = " ".join(err["details"]["missing"])
    assert "AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED" in missing_blob


def test_requires_configuration_when_signing_key_missing(monkeypatch):
    """Runner enabled but signing key missing → requires_configuration."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", "1")
    out = adversarial_red_teamer.run(_VALID_PAYLOAD)
    err = assert_error_envelope(
        out, "adversarial_red_teamer.requires_configuration",
    )
    missing_blob = " ".join(err["details"]["missing"])
    assert "AZTEA_REDTEAM_CONSENT_SIGNING_KEY" in missing_blob


def test_requires_configuration_lists_both_when_both_missing(monkeypatch):
    _clear_env(monkeypatch)
    out = adversarial_red_teamer.run(_VALID_PAYLOAD)
    err = assert_error_envelope(
        out, "adversarial_red_teamer.requires_configuration",
    )
    missing = err["details"]["missing"]
    blob = " ".join(missing)
    assert "AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED" in blob
    assert "AZTEA_REDTEAM_CONSENT_SIGNING_KEY" in blob


def test_happy_path_with_all_prerequisites(monkeypatch):
    set_env_for("redteam_configured", monkeypatch)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"categories":["fuzz"]}',
    ))
    out = adversarial_red_teamer.run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out


def test_reasoning_loop_two_calls(monkeypatch):
    set_env_for("redteam_configured", monkeypatch)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    adversarial_red_teamer.run(_VALID_PAYLOAD)
    assert len(calls) >= 2, f"expected >= 2 LLM calls, got {len(calls)}"


def test_budget_exceeded_returns_envelope(monkeypatch):
    set_env_for("redteam_configured", monkeypatch)

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=5,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = adversarial_red_teamer.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "adversarial_red_teamer.llm_error")
