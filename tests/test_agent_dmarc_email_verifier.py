"""test_agent_dmarc_email_verifier.py — C13 DMARC Email Verifier (~11 tests)."""
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

from agents import dmarc_email_verifier as _agent_mod


_VALID_PAYLOAD = {
    "sample_email": {"from": "noreply@x.com", "body": "hi"},
    "target_domains": ["recipient.com"],
}


def _clear_dmarc_env(monkeypatch):
    """Strip every env var the dmarc gate inspects so the test starts clean."""
    for v in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "AZTEA_DMARC_CANARY_INBOX"):
        monkeypatch.delenv(v, raising=False)


def test_invalid_input_envelope():
    out = _agent_mod.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "dmarc_email_verifier.invalid_input")


def test_missing_sample_email_rejected(monkeypatch):
    _clear_dmarc_env(monkeypatch)
    out = _agent_mod.run({"target_domains": ["recipient.com"]})
    err = assert_error_envelope(out, "dmarc_email_verifier.invalid_input")
    assert "sample_email" in err["message"]


def test_sample_email_must_have_from(monkeypatch):
    _clear_dmarc_env(monkeypatch)
    out = _agent_mod.run({
        "sample_email": {"body": "hi"},
        "target_domains": ["recipient.com"],
    })
    err = assert_error_envelope(out, "dmarc_email_verifier.invalid_input")
    assert "from" in err["message"]


def test_sample_email_must_have_body(monkeypatch):
    _clear_dmarc_env(monkeypatch)
    out = _agent_mod.run({
        "sample_email": {"from": "noreply@x.com"},
        "target_domains": ["recipient.com"],
    })
    err = assert_error_envelope(out, "dmarc_email_verifier.invalid_input")
    assert "body" in err["message"]


def test_target_domains_must_be_list(monkeypatch):
    _clear_dmarc_env(monkeypatch)
    out = _agent_mod.run({
        "sample_email": {"from": "noreply@x.com", "body": "hi"},
        "target_domains": "recipient.com",
    })
    err = assert_error_envelope(out, "dmarc_email_verifier.invalid_input")
    assert "target_domains" in err["message"]


def test_target_domains_empty_rejected(monkeypatch):
    _clear_dmarc_env(monkeypatch)
    out = _agent_mod.run({
        "sample_email": {"from": "noreply@x.com", "body": "hi"},
        "target_domains": [],
    })
    err = assert_error_envelope(out, "dmarc_email_verifier.invalid_input")
    assert "target_domains" in err["message"]


def test_requires_configuration_when_smtp_host_missing(monkeypatch):
    """No SMTP_HOST → gate fires with SMTP_HOST in the missing list."""
    _clear_dmarc_env(monkeypatch)
    out = _agent_mod.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out, "dmarc_email_verifier.requires_configuration")
    assert "SMTP_HOST" in " ".join(err["details"]["missing"])


def test_requires_configuration_when_smtp_user_missing(monkeypatch):
    """Partial SMTP config (HOST + PASS but no USER) still fires the gate."""
    _clear_dmarc_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example")
    monkeypatch.setenv("SMTP_PASS", "secret")
    monkeypatch.setenv("AZTEA_DMARC_CANARY_INBOX", "canary@example.com")
    out = _agent_mod.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out, "dmarc_email_verifier.requires_configuration")
    assert "SMTP_USER" in " ".join(err["details"]["missing"])


def test_requires_configuration_when_canary_inbox_missing(monkeypatch):
    """All SMTP set but missing AZTEA_DMARC_CANARY_INBOX → gate fires."""
    _clear_dmarc_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASS", "p")
    out = _agent_mod.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out, "dmarc_email_verifier.requires_configuration")
    assert "AZTEA_DMARC_CANARY_INBOX" in " ".join(err["details"]["missing"])


def test_happy_path_reaches_reasoning_loop(monkeypatch):
    set_env_for("dmarc_configured", monkeypatch)
    patch_llm_everywhere(
        monkeypatch,
        _stub_llm_factory('{"plan":"p","synthesis":"s","summary":"ok","verdict":"go"}'),
    )
    out = _agent_mod.run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out


def test_budget_exceeded_returns_envelope(monkeypatch):
    set_env_for("dmarc_configured", monkeypatch)

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=10,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = _agent_mod.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "dmarc_email_verifier.llm_error")
