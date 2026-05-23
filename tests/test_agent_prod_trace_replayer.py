"""test_agent_prod_trace_replayer.py — D18 Prod Trace Replayer (~10 tests)."""
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

from agents import prod_trace_replayer


def _clear_env(monkeypatch):
    monkeypatch.delenv("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", raising=False)


def _valid_payload(tmp_path) -> dict:
    return {
        "candidate_url": "https://staging.example.com",
        "trace_bundle_path": str(tmp_path / "bundle.json"),
    }


def test_invalid_input_envelope():
    out = prod_trace_replayer.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "prod_trace_replayer.invalid_input")


def test_missing_candidate_url_rejected(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    out = prod_trace_replayer.run(
        {"trace_bundle_path": str(tmp_path / "bundle.json")}
    )
    err = assert_error_envelope(out, "prod_trace_replayer.invalid_input")
    assert "candidate_url" in err["message"]


def test_missing_bundle_path_rejected(monkeypatch):
    _clear_env(monkeypatch)
    out = prod_trace_replayer.run(
        {"candidate_url": "https://staging.example.com"}
    )
    err = assert_error_envelope(out, "prod_trace_replayer.invalid_input")
    assert "trace_bundle_path" in err["message"]


def test_bundle_path_must_exist(monkeypatch, tmp_path):
    """A non-existent bundle path → requires_configuration mentioning the path."""
    _clear_env(monkeypatch)
    missing_path = str(tmp_path / "does_not_exist.json")
    out = prod_trace_replayer.run({
        "candidate_url": "https://staging.example.com",
        "trace_bundle_path": missing_path,
    })
    err = assert_error_envelope(
        out, "prod_trace_replayer.requires_configuration",
    )
    missing_blob = " ".join(err["details"]["missing"])
    assert missing_path in missing_blob


def test_requires_configuration_when_runner_disabled(monkeypatch, tmp_path):
    """Bundle file exists but the runner flag is off → requires_configuration."""
    _clear_env(monkeypatch)
    bundle = tmp_path / "bundle.json"
    bundle.write_text('{"requests":[]}')
    out = prod_trace_replayer.run({
        "candidate_url": "https://staging.example.com",
        "trace_bundle_path": str(bundle),
    })
    err = assert_error_envelope(
        out, "prod_trace_replayer.requires_configuration",
    )
    missing_blob = " ".join(err["details"]["missing"])
    assert "AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED" in missing_blob


def test_happy_path_with_runner_and_bundle(monkeypatch, tmp_path):
    """Env enabled + bundle present → reaches reasoning loop, returns success."""
    set_env_for("prod_trace_replayer_configured", monkeypatch)
    bundle = tmp_path / "bundle.json"
    bundle.write_text('{"requests":[]}')
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"slice_by":["route"],"sample_rate":0.1}',
    ))
    out = prod_trace_replayer.run({
        "candidate_url": "https://staging.example.com",
        "trace_bundle_path": str(bundle),
    })
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out


def test_reasoning_loop_two_calls(monkeypatch, tmp_path):
    set_env_for("prod_trace_replayer_configured", monkeypatch)
    bundle = tmp_path / "bundle.json"
    bundle.write_text('{"requests":[]}')
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    prod_trace_replayer.run({
        "candidate_url": "https://staging.example.com",
        "trace_bundle_path": str(bundle),
    })
    assert len(calls) >= 2, f"expected >= 2 LLM calls, got {len(calls)}"


def test_budget_exceeded_returns_envelope(monkeypatch, tmp_path):
    set_env_for("prod_trace_replayer_configured", monkeypatch)
    bundle = tmp_path / "bundle.json"
    bundle.write_text('{"requests":[]}')

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=5,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = prod_trace_replayer.run({
        "candidate_url": "https://staging.example.com",
        "trace_bundle_path": str(bundle),
    })
    assert_error_envelope(out, "prod_trace_replayer.llm_error")
