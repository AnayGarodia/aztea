"""Unit tests for the `aztea dispute` typer command + back-compat sub-app.

Covers the CLI surface — flag combinations, exit codes, JSON mode, status
mode, dry-run, --yes, fallback estimates. Wizard UX, error mapping, and
robustness live in their own files (`test_cli_dispute_wizard.py`,
`test_cli_dispute_errors.py`, `test_cli_dispute_robustness.py`).
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from aztea.cli import app
from aztea.errors import APIError, ConflictError, NotFoundError

from ._dispute_helpers import (
    DEFAULT_POLICY,
    FakeDisputeClient,
    patch_client,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# Direct-mode happy paths
# ---------------------------------------------------------------------------


def test_dispute_direct_mode_files_with_yes_flag(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "stale data", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls == [
        {"job_id": "job-1", "reason": "stale data", "evidence": None}
    ]


def test_dispute_passes_evidence_through(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app,
        [
            "dispute", "job-1",
            "--reason", "missing CVE",
            "--evidence", "https://x",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["evidence"] == "https://x"


def test_dispute_dry_run_does_not_file(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--dry-run", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls == []


def test_dispute_dry_run_text_mode_prints_preview(monkeypatch) -> None:
    """Without --yes the preview renders before the dry-run short-circuit."""
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert "filing deposit" in result.output.lower()
    assert fake.dispute_calls == []


# ---------------------------------------------------------------------------
# JSON mode
# ---------------------------------------------------------------------------


def test_dispute_json_mode_emits_receipt(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dispute_id"] == "dsp_1"
    assert payload["job_id"] == "job-1"


def test_dispute_json_mode_dry_run_emits_estimate(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    # 100¢ * 500 bps / 10000 = 5¢; equals min_cents.
    assert payload["estimated_deposit_cents"] == 5
    assert fake.dispute_calls == []


def test_dispute_json_mode_requires_reason(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "job-1", "--json"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# --status mode
# ---------------------------------------------------------------------------


def test_dispute_status_renders_existing(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--status", "job-1", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dispute_id"] == "dsp_1"
    assert fake.dispute_calls == []


def test_dispute_status_unknown_job_404_renders_clean(monkeypatch) -> None:
    err = NotFoundError(
        status_code=404,
        message="No dispute found for this job.",
        detail=None,
        body=None,
        code="not_found",
    )
    fake = FakeDisputeClient(get_dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--status", "job-bogus"])
    assert result.exit_code == 1
    # No traceback in user-facing output.
    assert "Traceback" not in result.output


def test_dispute_status_with_judges_renders_vote_count(monkeypatch) -> None:
    fake = FakeDisputeClient(
        get_dispute_result={
            "dispute_id": "dsp_1",
            "job_id": "job-1",
            "status": "pending",
            "side": "caller",
            "filed_at": "2026-05-09T00:00:00+00:00",
            "judgments": [{"vote": "agent"}, {"vote": "caller"}],
        }
    )
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--status", "job-1"])
    assert result.exit_code == 0, result.output
    assert "2 voted" in result.output


def test_dispute_status_renders_outcome_when_present(monkeypatch) -> None:
    fake = FakeDisputeClient(
        get_dispute_result={
            "dispute_id": "dsp_1",
            "job_id": "job-1",
            "status": "final",
            "side": "caller",
            "filed_at": "2026-05-09T00:00:00+00:00",
            "outcome": "agent_wins",
            "judgments": [],
        }
    )
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--status", "job-1"])
    assert result.exit_code == 0, result.output
    assert "outcome" in result.output
    assert "agent_wins" in result.output


def test_dispute_status_truncates_long_evidence(monkeypatch) -> None:
    long_text = "x" * 250
    fake = FakeDisputeClient(
        get_dispute_result={
            "dispute_id": "dsp_1",
            "job_id": "job-1",
            "status": "pending",
            "side": "caller",
            "filed_at": "2026-05-09T00:00:00+00:00",
            "evidence": long_text,
            "judgments": [],
        }
    )
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--status", "job-1"])
    assert result.exit_code == 0, result.output
    # Truncation marker indicates the evidence row was shortened.
    assert "…" in result.output or "..." in result.output


def test_dispute_status_json_mode_emits_raw_record(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--status", "job-1", "--json"])
    payload = json.loads(result.stdout)
    # Raw record has dispute_id + judgments — proves no envelope wrapping.
    assert "dispute_id" in payload
    assert "judgments" in payload


# ---------------------------------------------------------------------------
# Wizard-mode gates (exhaustive wizard tests live in test_cli_dispute_wizard.py)
# ---------------------------------------------------------------------------


def test_dispute_wizard_refuses_in_json_mode(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--json"])
    assert result.exit_code == 2
    assert fake.dispute_calls == []


def test_dispute_wizard_refuses_when_not_tty(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute"])
    assert result.exit_code == 2


def test_dispute_no_args_no_tty_exits_with_helpful_message(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute"])
    assert result.exit_code == 2
    # Tells the user what to do instead.
    assert (
        "interactive terminal" in result.output
        or "TTY" in result.output
        or "Pass" in result.output
    )


# ---------------------------------------------------------------------------
# Help + flags
# ---------------------------------------------------------------------------


def test_dispute_help_lists_all_flags() -> None:
    result = runner.invoke(app, ["dispute", "--help"])
    assert result.exit_code == 0
    for flag in ("--reason", "--evidence", "--status", "--dry-run", "--yes",
                 "--limit", "--api-key", "--base-url", "--json"):
        assert flag in result.output, f"--help is missing {flag}"


def test_dispute_yes_flag_skips_confirmation(monkeypatch) -> None:
    """`--yes` must not prompt. We assert that by making any prompt call raise."""
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)

    def _explode(*args, **kwargs):
        raise AssertionError("Confirm.ask should NOT be called when --yes is set")

    monkeypatch.setattr("rich.prompt.Confirm.ask", _explode)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert len(fake.dispute_calls) == 1


# ---------------------------------------------------------------------------
# Receipt / output details
# ---------------------------------------------------------------------------


def test_dispute_emit_receipt_includes_track_command(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "aztea dispute --status job-1" in result.output


def test_dispute_uses_open_client_indirection(monkeypatch) -> None:
    """`aztea.cli._client` must be the patch point — not `build_client` directly."""
    fake = FakeDisputeClient()
    monkeypatch.setattr("aztea.cli._client", lambda **_: fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls, "filing did not route through patched _client"


# ---------------------------------------------------------------------------
# Deposit estimation fallbacks
# ---------------------------------------------------------------------------


def test_dispute_estimates_deposit_when_policy_endpoint_fails(monkeypatch) -> None:
    """Policy endpoint is best-effort: if it raises, preview falls back."""
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
        app, ["dispute", "job-1", "--reason", "test", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Falls back to the same default constants; 100¢ price gives 5¢ deposit.
    assert payload["estimated_deposit_cents"] == 5


def test_dispute_estimates_deposit_when_get_job_fails(monkeypatch) -> None:
    """If the job-price lookup fails the preview uses the min-cents floor."""
    err = APIError(
        status_code=500,
        message="boom",
        detail=None,
        body=None,
        code="server.error",
    )
    fake = FakeDisputeClient(get_job_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Without a price, deposit floors at min_cents (5).
    assert payload["estimated_deposit_cents"] == 5


def test_dispute_estimates_deposit_for_high_priced_job(monkeypatch) -> None:
    """For a $5.00 (500¢) job: 500 * 500 / 10000 = 25¢ — above min."""
    fake = FakeDisputeClient(get_job_result={"price_cents": 500})
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--dry-run", "--json"]
    )
    payload = json.loads(result.stdout)
    assert payload["estimated_deposit_cents"] == 25


# ---------------------------------------------------------------------------
# Error mapping (smoke; deeper coverage in test_cli_dispute_errors.py)
# ---------------------------------------------------------------------------


def test_dispute_already_filed_renders_friendly_hint(monkeypatch) -> None:
    err = ConflictError(
        status_code=409,
        message="A dispute already exists for this job.",
        detail=None,
        body=None,
        code="dispute.already_filed",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    assert "aztea dispute --status" in result.output


def test_dispute_window_expired_renders_friendly_hint(monkeypatch) -> None:
    err = APIError(
        status_code=400,
        message="Dispute window has expired for this job.",
        detail=None,
        body=None,
        code="dispute.window_expired",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Back-compat: aztea jobs dispute
# ---------------------------------------------------------------------------


def test_jobs_dispute_subcommand_still_works(monkeypatch) -> None:
    fake = FakeDisputeClient()
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["jobs", "dispute", "job-1", "--reason", "test", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dispute_id"] == "dsp_1"
    assert fake.dispute_calls == [
        {"job_id": "job-1", "reason": "test", "evidence": None}
    ]


def test_jobs_dispute_subcommand_signature_unchanged() -> None:
    """`aztea jobs dispute --help` must still list the original flags."""
    result = runner.invoke(app, ["jobs", "dispute", "--help"])
    assert result.exit_code == 0
    for flag in ("--reason", "--evidence", "--api-key", "--base-url", "--json"):
        assert flag in result.output, f"sub-app help is missing {flag}"
