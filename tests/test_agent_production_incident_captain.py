"""test_agent_production_incident_captain.py — C15 Production Incident Captain (~12 tests)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core.llm.errors import BudgetExceededError, LLMError
from tests.agent_helpers import (
    _capture_llm_calls,
    _stub_llm_factory,
    assert_error_envelope,
    patch_llm_everywhere,
    set_env_for,
)

from agents import production_incident_captain as _agent_mod


_VALID_PAYLOAD = {"page_id": "PD-12345"}


def _clear_incident_env(monkeypatch):
    """Strip env vars the incident-captain gate inspects so tests start clean."""
    for v in ("PAGERDUTY_API_TOKEN", "SENTRY_API_TOKEN",
              "AZTEA_INCIDENT_DOC_TARGET"):
        monkeypatch.delenv(v, raising=False)


def test_invalid_input_envelope():
    out = _agent_mod.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "production_incident_captain.invalid_input")


def test_missing_page_id_rejected(monkeypatch):
    _clear_incident_env(monkeypatch)
    out = _agent_mod.run({})
    err = assert_error_envelope(out, "production_incident_captain.invalid_input")
    assert "page_id" in err["message"]


def test_empty_page_id_rejected(monkeypatch):
    _clear_incident_env(monkeypatch)
    out = _agent_mod.run({"page_id": "   "})
    err = assert_error_envelope(out, "production_incident_captain.invalid_input")
    assert "page_id" in err["message"]


def test_escalation_threshold_lower_bound(monkeypatch):
    """A negative threshold is out of (0, 1] and must be rejected.
    Note: literal 0.0 is falsy in Python so ``x or default`` coerces it to
    the default — we use -0.1 to exercise the explicit bounds check."""
    _clear_incident_env(monkeypatch)
    out = _agent_mod.run({
        "page_id": "PD-1", "escalation_confidence_threshold": -0.1,
    })
    err = assert_error_envelope(out, "production_incident_captain.invalid_input")
    assert "escalation_confidence_threshold" in err["message"]


def test_escalation_threshold_upper_bound(monkeypatch):
    """1.5 is out of (0, 1] and must be rejected."""
    _clear_incident_env(monkeypatch)
    out = _agent_mod.run({
        "page_id": "PD-1", "escalation_confidence_threshold": 1.5,
    })
    err = assert_error_envelope(out, "production_incident_captain.invalid_input")
    assert "escalation_confidence_threshold" in err["message"]


def test_escalation_threshold_one_accepted(monkeypatch):
    """1.0 is the inclusive upper bound and must pass validation."""
    _clear_incident_env(monkeypatch)
    out = _agent_mod.run({
        "page_id": "PD-1", "escalation_confidence_threshold": 1.0,
    })
    # Validation passes; without env we hit the config gate.
    assert_error_envelope(
        out, "production_incident_captain.requires_configuration",
    )


def test_default_escalation_threshold_used_when_omitted(monkeypatch):
    """Omitting the threshold uses the agent default (0.7) — validation passes
    and we fall through to the config gate."""
    _clear_incident_env(monkeypatch)
    out = _agent_mod.run({"page_id": "PD-1"})
    assert_error_envelope(
        out, "production_incident_captain.requires_configuration",
    )


def test_requires_configuration_when_pagerduty_missing(monkeypatch):
    """No PAGERDUTY_API_TOKEN → gate fires with PD in the missing list."""
    _clear_incident_env(monkeypatch)
    monkeypatch.setenv("SENTRY_API_TOKEN", "tok-sentry")
    monkeypatch.setenv("AZTEA_INCIDENT_DOC_TARGET", "https://docs.example/war-room")
    out = _agent_mod.run(_VALID_PAYLOAD)
    err = assert_error_envelope(
        out, "production_incident_captain.requires_configuration",
    )
    assert "PAGERDUTY_API_TOKEN" in " ".join(err["details"]["missing"])


def test_requires_configuration_when_sentry_missing(monkeypatch):
    _clear_incident_env(monkeypatch)
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "tok-pd")
    monkeypatch.setenv("AZTEA_INCIDENT_DOC_TARGET", "https://docs.example/war-room")
    out = _agent_mod.run(_VALID_PAYLOAD)
    err = assert_error_envelope(
        out, "production_incident_captain.requires_configuration",
    )
    assert "SENTRY_API_TOKEN" in " ".join(err["details"]["missing"])


def test_requires_configuration_when_doc_target_missing(monkeypatch):
    _clear_incident_env(monkeypatch)
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "tok-pd")
    monkeypatch.setenv("SENTRY_API_TOKEN", "tok-sentry")
    out = _agent_mod.run(_VALID_PAYLOAD)
    err = assert_error_envelope(
        out, "production_incident_captain.requires_configuration",
    )
    assert "AZTEA_INCIDENT_DOC_TARGET" in " ".join(err["details"]["missing"])


def test_happy_path_reaches_reasoning_loop(monkeypatch):
    set_env_for("incident_captain_configured", monkeypatch)
    patch_llm_everywhere(
        monkeypatch,
        _stub_llm_factory('{"plan":"p","synthesis":"s","summary":"ok","verdict":"go"}'),
    )
    out = _agent_mod.run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out


def test_budget_exceeded_returns_envelope(monkeypatch):
    set_env_for("incident_captain_configured", monkeypatch)

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=10,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = _agent_mod.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "production_incident_captain.llm_error")
