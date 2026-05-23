"""test_agent_privacy_flow_tracer.py — E24 Privacy Flow Tracer (~10 tests)."""
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

from agents import privacy_flow_tracer


_VALID_PAYLOAD = {
    "repo_root": "/tmp/app",
    "pii_tags": ["ssn", "email"],
}


def _clear_env(monkeypatch):
    monkeypatch.delenv("AZTEA_OTEL_COLLECTOR_URL", raising=False)
    monkeypatch.delenv("AZTEA_EBPF_AGENT_SOCKET", raising=False)


def test_invalid_input_envelope():
    out = privacy_flow_tracer.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "privacy_flow_tracer.invalid_input")


def test_missing_repo_root_rejected(monkeypatch):
    _clear_env(monkeypatch)
    out = privacy_flow_tracer.run({"pii_tags": ["ssn"]})
    err = assert_error_envelope(out, "privacy_flow_tracer.invalid_input")
    assert "repo_root" in err["message"]


def test_relative_repo_root_rejected(monkeypatch):
    _clear_env(monkeypatch)
    out = privacy_flow_tracer.run({"repo_root": "../app", "pii_tags": ["ssn"]})
    err = assert_error_envelope(out, "privacy_flow_tracer.invalid_input")
    assert "repo_root" in err["message"]


def test_missing_pii_tags_rejected(monkeypatch):
    _clear_env(monkeypatch)
    out = privacy_flow_tracer.run({"repo_root": "/tmp/app"})
    err = assert_error_envelope(out, "privacy_flow_tracer.invalid_input")
    assert "pii_tags" in err["message"]


def test_pii_tags_must_be_list(monkeypatch):
    _clear_env(monkeypatch)
    out = privacy_flow_tracer.run({
        "repo_root": "/tmp/app", "pii_tags": "ssn,email",
    })
    err = assert_error_envelope(out, "privacy_flow_tracer.invalid_input")
    assert "pii_tags" in err["message"]


def test_pii_tags_empty_rejected(monkeypatch):
    _clear_env(monkeypatch)
    out = privacy_flow_tracer.run({"repo_root": "/tmp/app", "pii_tags": []})
    err = assert_error_envelope(out, "privacy_flow_tracer.invalid_input")
    assert "pii_tags" in err["message"]


def test_requires_configuration_when_otel_missing(monkeypatch):
    _clear_env(monkeypatch)
    out = privacy_flow_tracer.run(_VALID_PAYLOAD)
    err = assert_error_envelope(
        out, "privacy_flow_tracer.requires_configuration",
    )
    missing_blob = " ".join(err["details"]["missing"])
    assert "AZTEA_OTEL_COLLECTOR_URL" in missing_blob


def test_requires_configuration_when_ebpf_missing(monkeypatch):
    """OTel present but eBPF socket missing → still requires_configuration."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("AZTEA_OTEL_COLLECTOR_URL", "https://otel.example/v1")
    out = privacy_flow_tracer.run(_VALID_PAYLOAD)
    err = assert_error_envelope(
        out, "privacy_flow_tracer.requires_configuration",
    )
    missing_blob = " ".join(err["details"]["missing"])
    assert "AZTEA_EBPF_AGENT_SOCKET" in missing_blob


def test_happy_path_with_both_signal_sources(monkeypatch):
    set_env_for("privacy_tracer_configured", monkeypatch)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"sources":[]}',
    ))
    out = privacy_flow_tracer.run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out


def test_reasoning_loop_two_calls(monkeypatch):
    set_env_for("privacy_tracer_configured", monkeypatch)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    privacy_flow_tracer.run(_VALID_PAYLOAD)
    assert len(calls) >= 2, f"expected >= 2 LLM calls, got {len(calls)}"
