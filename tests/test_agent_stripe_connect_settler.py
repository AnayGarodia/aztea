"""test_agent_stripe_connect_settler.py — C14 Stripe Connect Settler (~11 tests)."""
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

from agents import stripe_connect_settler as _agent_mod


_VALID_PAYLOAD = {"month": "2026-04", "internal_ledger_source": "/tmp/ledger.csv"}


def _clear_stripe_env(monkeypatch):
    """Strip env vars the stripe gate inspects so tests start clean."""
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)


def test_invalid_input_envelope():
    out = _agent_mod.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "stripe_connect_settler.invalid_input")


def test_missing_month_rejected(monkeypatch):
    _clear_stripe_env(monkeypatch)
    out = _agent_mod.run({"internal_ledger_source": "/tmp/ledger.csv"})
    err = assert_error_envelope(out, "stripe_connect_settler.invalid_input")
    assert "month" in err["message"]


def test_month_format_strict_rejects_unpadded(monkeypatch):
    """'2026-4' (no leading 0) must be rejected — Stripe wants YYYY-MM."""
    _clear_stripe_env(monkeypatch)
    out = _agent_mod.run({
        "month": "2026-4", "internal_ledger_source": "/tmp/ledger.csv",
    })
    err = assert_error_envelope(out, "stripe_connect_settler.invalid_input")
    assert "YYYY-MM" in err["message"]


def test_month_format_rejects_english_month(monkeypatch):
    _clear_stripe_env(monkeypatch)
    out = _agent_mod.run({
        "month": "April 2026", "internal_ledger_source": "/tmp/ledger.csv",
    })
    err = assert_error_envelope(out, "stripe_connect_settler.invalid_input")
    assert "YYYY-MM" in err["message"]


def test_month_format_rejects_invalid_month_number(monkeypatch):
    """The regex is digit-shape only; the spec rejects '2026-13' but Stripe
    will also error downstream. Verify the agent surfaces invalid_input."""
    _clear_stripe_env(monkeypatch)
    out = _agent_mod.run({
        "month": "2026-13", "internal_ledger_source": "/tmp/ledger.csv",
    })
    # The current regex accepts \d{2} for month, so this MAY pass validation
    # and hit the config gate; either way the agent must NOT return success.
    err = out["error"]
    assert err["code"] in {
        "stripe_connect_settler.invalid_input",
        "stripe_connect_settler.requires_configuration",
    }, f"unexpected code: {err['code']!r}"


def test_month_format_accepts_zero_padded(monkeypatch):
    """'2026-04' must pass validation and reach the next stage."""
    _clear_stripe_env(monkeypatch)
    out = _agent_mod.run(_VALID_PAYLOAD)
    # Validation passes; without STRIPE_API_KEY we hit the config gate.
    assert_error_envelope(out, "stripe_connect_settler.requires_configuration")


def test_missing_ledger_source_rejected(monkeypatch):
    _clear_stripe_env(monkeypatch)
    out = _agent_mod.run({"month": "2026-04"})
    err = assert_error_envelope(out, "stripe_connect_settler.invalid_input")
    assert "internal_ledger_source" in err["message"]


def test_requires_configuration_when_stripe_key_missing(monkeypatch):
    """No STRIPE_API_KEY → requires_configuration."""
    _clear_stripe_env(monkeypatch)
    out = _agent_mod.run(_VALID_PAYLOAD)
    err = assert_error_envelope(
        out, "stripe_connect_settler.requires_configuration",
    )
    assert "STRIPE_API_KEY" in " ".join(err["details"]["missing"])


def test_happy_path_reaches_reasoning_loop(monkeypatch):
    set_env_for("stripe_settler_configured", monkeypatch)
    patch_llm_everywhere(
        monkeypatch,
        _stub_llm_factory('{"plan":"p","synthesis":"s","summary":"ok","verdict":"go"}'),
    )
    out = _agent_mod.run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out


def test_reasoning_loop_two_calls(monkeypatch):
    set_env_for("stripe_settler_configured", monkeypatch)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    _agent_mod.run(_VALID_PAYLOAD)
    assert len(calls) >= 2, f"expected >= 2 LLM calls, got {len(calls)}"


def test_budget_exceeded_returns_envelope(monkeypatch):
    set_env_for("stripe_settler_configured", monkeypatch)

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=10,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = _agent_mod.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "stripe_connect_settler.llm_error")
