"""Adversarial / malformed-input tests for `aztea dispute`.

Mirrors `tests/test_listing_safety_robustness.py` style: feed the CLI
inputs that real users will produce (unicode, very long strings, weird
URLs) plus inputs the server might produce when something goes wrong
(missing keys, wrong types, empty payloads). Goal: no traceback, no
crash, no silent corruption.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from aztea.cli import app
from aztea.errors import APIError, ConflictError

from ._dispute_helpers import FakeDisputeClient, make_job, patch_client, patch_tty


runner = CliRunner()


# ---------------------------------------------------------------------------
# Reason / evidence content
# ---------------------------------------------------------------------------


def test_reason_with_unicode_passes_through(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app,
        ["dispute", "job-1", "--reason", "café résumé naïve test", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["reason"] == "café résumé naïve test"


def test_reason_with_emoji_passes_through(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "🐛 bug found here", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "🐛" in fake.dispute_calls[0]["reason"]


def test_reason_with_newlines_preserved(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    reason = "line one\nline two\nline three"
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", reason, "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["reason"] == reason


def test_very_long_reason_passed_full_to_server(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    reason = ("very long reason text " * 100).strip()
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", reason, "--yes"]
    )
    assert result.exit_code == 0, result.output
    # Server receives the full reason; preview/display truncates separately.
    assert fake.dispute_calls[0]["reason"] == reason


def test_reason_with_quotes_preserved(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    reason = 'She said "hello" — quoted for emphasis.'
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", reason, "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["reason"] == reason


def test_evidence_with_special_url_chars(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    evidence = "https://example.com/path?q=a%20b&n=1#frag"
    result = runner.invoke(
        app,
        ["dispute", "job-1", "--reason", "test", "--evidence", evidence, "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["evidence"] == evidence


# ---------------------------------------------------------------------------
# Job ID handling
# ---------------------------------------------------------------------------


def test_job_id_with_spaces_passed_through_as_is(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job 1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["job_id"] == "job 1"


# ---------------------------------------------------------------------------
# Filing response shapes
# ---------------------------------------------------------------------------


def test_filing_response_missing_dispute_id(monkeypatch) -> None:
    """Server returns empty dict — receipt should render with `—` placeholders."""
    fake = FakeDisputeClient(dispute_result={})
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output


def test_filing_response_with_extra_fields(monkeypatch) -> None:
    fake = FakeDisputeClient(
        dispute_result={
            "dispute_id": "dsp_x",
            "status": "pending",
            "side": "caller",
            "filing_deposit_cents": 5,
            "extra_field": "ignored",
            "another_extra": [1, 2, 3],
        }
    )
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Extra fields preserved in raw payload.
    assert payload["raw"]["extra_field"] == "ignored"


def test_filing_response_empty_dict_in_json_mode(monkeypatch) -> None:
    fake = FakeDisputeClient(dispute_result={})
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--json"]
    )
    assert result.exit_code == 0, result.output
    # Must be parseable JSON.
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# Status response shapes
# ---------------------------------------------------------------------------


def test_status_response_missing_judgments(monkeypatch) -> None:
    fake = FakeDisputeClient(
        get_dispute_result={
            "dispute_id": "dsp_x",
            "job_id": "job-1",
            "status": "pending",
            "side": "caller",
            "filed_at": "2026-05-09T00:00:00+00:00",
        }
    )
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--status", "job-1"])
    assert result.exit_code == 0, result.output
    assert "0 voted" in result.output


def test_status_response_with_non_dict_judgment(monkeypatch) -> None:
    fake = FakeDisputeClient(
        get_dispute_result={
            "dispute_id": "dsp_x",
            "job_id": "job-1",
            "status": "pending",
            "side": "caller",
            "filed_at": "2026-05-09T00:00:00+00:00",
            "judgments": [None, "weird", {"vote": "agent"}],
        }
    )
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--status", "job-1"])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output


def test_status_response_with_missing_optional_fields(monkeypatch) -> None:
    fake = FakeDisputeClient(
        get_dispute_result={"dispute_id": "dsp_x", "job_id": "job-1"}
    )
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--status", "job-1"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# list_jobs / wizard response shapes
# ---------------------------------------------------------------------------


def test_list_jobs_response_missing_jobs_key(monkeypatch) -> None:
    fake = FakeDisputeClient(list_jobs_response={})
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    assert result.exit_code == 0, result.output
    assert "No recent jobs" in result.output


def test_list_jobs_response_jobs_is_None(monkeypatch) -> None:
    fake = FakeDisputeClient(list_jobs_response={"jobs": None})
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    assert result.exit_code == 0, result.output


def test_list_jobs_response_not_a_dict(monkeypatch) -> None:
    fake = FakeDisputeClient(list_jobs_response=[])  # type: ignore[arg-type]
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    # Wizard treats non-dict as empty; should not crash.
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Policy endpoint failure modes
# ---------------------------------------------------------------------------


def test_policy_endpoint_returns_partial_data(monkeypatch) -> None:
    """Policy without `filing_deposit_bps` falls back to default."""
    fake = FakeDisputeClient(policy={"filing_deposit_min_cents": 5})
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Default 500 bps applied; for 100c price → max(5, 5) = 5
    assert payload["estimated_deposit_cents"] >= 5


def test_policy_endpoint_returns_negative_bps(monkeypatch) -> None:
    """Negative bps treated as default (defensive)."""
    fake = FakeDisputeClient(policy={"filing_deposit_bps": -1, "filing_deposit_min_cents": 5})
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.output


def test_policy_endpoint_raises_500(monkeypatch) -> None:
    err = APIError(
        status_code=500,
        message="boom",
        detail=None,
        body=None,
        code="server.error",
    )
    fake = FakeDisputeClient(policy_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    # Filing still proceeds with fallback estimate.
    assert result.exit_code == 0, result.output
    assert len(fake.dispute_calls) == 1


def test_policy_endpoint_returns_non_dict(monkeypatch) -> None:
    """Server returns a list; CLI must not crash."""
    fake = FakeDisputeClient(policy=[])  # type: ignore[arg-type]
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Concurrency / race
# ---------------------------------------------------------------------------


def test_dispute_filed_returns_409_after_picker_selection(monkeypatch) -> None:
    """Wizard picks job; meanwhile someone else files; server returns 409."""
    err = ConflictError(
        status_code=409,
        message="A dispute already exists for this job.",
        detail=None,
        body=None,
        code="dispute.already_filed",
    )
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None},
        dispute_raises=err,
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "y\nthree word reason here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 1
    # Friendly hint suggests checking status.
    assert "aztea dispute --status" in result.output


# ---------------------------------------------------------------------------
# Prompt input edge cases
# ---------------------------------------------------------------------------


def test_wizard_handles_extra_whitespace_in_reason(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    # Reason with leading/trailing whitespace should be stripped.
    stdin = "y\n   three padded words   \n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["reason"] == "three padded words"


def test_wizard_handles_evidence_with_only_whitespace(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    # Evidence "   " strips to empty → treated as None (skipped).
    stdin = "y\nthree word reason here\n   \ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["evidence"] is None
