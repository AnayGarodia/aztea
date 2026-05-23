"""test_agent_pr_watch.py — A5 PR Watch (~9 tests)."""
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

from agents import pr_watch


_VALID_PAYLOAD = {"pr_url": "https://github.com/owner/repo/pull/1"}


def _clear_pr_watch_env(monkeypatch):
    monkeypatch.delenv("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    # Force is_configured() to return False unless a test explicitly opts in.
    monkeypatch.setattr(
        "core.hosted_index.github_app.is_configured", lambda: False,
    )


def test_invalid_input_envelope():
    out = pr_watch.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "pr_watch.invalid_input")


def test_missing_pr_url_rejected(monkeypatch):
    _clear_pr_watch_env(monkeypatch)
    out = pr_watch.run({})
    err = assert_error_envelope(out, "pr_watch.invalid_input")
    assert "pr_url" in err["message"]


def test_non_github_url_rejected(monkeypatch):
    _clear_pr_watch_env(monkeypatch)
    out = pr_watch.run({"pr_url": "https://gitlab.com/foo/bar/-/merge_requests/1"})
    err = assert_error_envelope(out, "pr_watch.invalid_input")
    assert "github" in err["message"].lower()


def test_non_https_url_rejected(monkeypatch):
    _clear_pr_watch_env(monkeypatch)
    out = pr_watch.run({"pr_url": "ftp://github.com/owner/repo/pull/1"})
    assert_error_envelope(out, "pr_watch.invalid_input")


def test_watch_seconds_clamped_to_86400(monkeypatch):
    """A huge watch_seconds is accepted; the value is clamped to <= 86400."""
    _clear_pr_watch_env(monkeypatch)
    out = pr_watch.run({**_VALID_PAYLOAD, "watch_seconds": 9_999_999})
    err = assert_error_envelope(out, "pr_watch.requires_configuration")
    assert err["details"]["watch_seconds"] <= 86400


def test_requires_configuration_when_github_app_missing(monkeypatch):
    """Runner env set; GitHub App still unconfigured → requires_configuration."""
    monkeypatch.setenv("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", "1")
    monkeypatch.setattr(
        "core.hosted_index.github_app.is_configured", lambda: False,
    )
    out = pr_watch.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out, "pr_watch.requires_configuration")
    missing_blob = " ".join(err["details"]["missing"])
    assert "GITHUB_APP" in missing_blob


def test_requires_configuration_when_runner_missing(monkeypatch):
    """GitHub App configured; runner env still unset → requires_configuration."""
    monkeypatch.delenv("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", raising=False)
    monkeypatch.setattr(
        "core.hosted_index.github_app.is_configured", lambda: True,
    )
    out = pr_watch.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out, "pr_watch.requires_configuration")
    missing_blob = " ".join(err["details"]["missing"])
    assert "AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED" in missing_blob


def test_happy_path_with_both_configured(monkeypatch):
    """Both runner env and GitHub App configured → reasoning loop runs."""
    set_env_for("pr_watch_configured", monkeypatch)
    monkeypatch.setattr(
        "core.hosted_index.github_app.is_configured", lambda: True,
    )
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"events": [], "final_state": "merged"}',
    ))
    out = pr_watch.run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "plan" in out and "synthesis" in out and "trace" in out
    assert_reasoning_loop(out)


def test_budget_exceeded_returns_envelope(monkeypatch):
    set_env_for("pr_watch_configured", monkeypatch)
    monkeypatch.setattr(
        "core.hosted_index.github_app.is_configured", lambda: True,
    )

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=5,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = pr_watch.run(_VALID_PAYLOAD)
    assert_error_envelope(out, "pr_watch.llm_error")
