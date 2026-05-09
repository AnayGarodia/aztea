"""Wizard tests — drive `aztea dispute` (no job_id) via Typer's CliRunner.

The wizard fetches recent jobs, renders a numbered picker over the
disputable ones, and walks the user through reason + evidence + confirm.
We mock _is_tty + the SDK client so tests stay fully in-process.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from aztea.cli import app

from ._dispute_helpers import (
    FakeDisputeClient,
    make_ineligible_job,
    make_job,
    patch_client,
    patch_tty,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# Empty / degenerate states
# ---------------------------------------------------------------------------


def test_wizard_empty_state_when_no_jobs(monkeypatch) -> None:
    fake = FakeDisputeClient(list_jobs_response={"jobs": [], "next_cursor": None})
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    assert result.exit_code == 0, result.output
    assert "No recent jobs" in result.output
    assert "aztea hire" in result.output
    assert fake.dispute_calls == []


def test_wizard_all_ineligible_jobs_renders_reasons(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [
                make_ineligible_job(code="dispute.already_filed", job_id="j1"),
                make_ineligible_job(code="dispute.already_rated", job_id="j2"),
                make_ineligible_job(code="dispute.window_expired", job_id="j3"),
            ],
            "next_cursor": None,
        }
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    assert result.exit_code == 0, result.output
    assert "already exists" in result.output
    assert "already rated" in result.output
    assert "window has expired" in result.output
    assert fake.dispute_calls == []


def test_wizard_all_ineligible_renders_tip(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [make_ineligible_job(code="dispute.already_rated")],
            "next_cursor": None,
        }
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    assert result.exit_code == 0, result.output
    assert "before rating" in result.output or "within the dispute window" in result.output


# ---------------------------------------------------------------------------
# Single-job auto-confirm
# ---------------------------------------------------------------------------


def test_wizard_single_eligible_offers_confirm_yes(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="job-only")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    # stdin: confirm y, reason, evidence skip, confirm y
    stdin = "y\nthree word reason here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls == [
        {
            "job_id": "job-only",
            "reason": "three word reason here",
            "evidence": None,
        }
    ]


def test_wizard_single_eligible_decline_exits_clean(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="job-only")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls == []


# ---------------------------------------------------------------------------
# Numeric pick
# ---------------------------------------------------------------------------


def _three_eligible_jobs() -> list[dict]:
    return [
        make_job(job_id="job-A", agent_name="alpha"),
        make_job(job_id="job-B", agent_name="beta"),
        make_job(job_id="job-C", agent_name="gamma"),
    ]


def test_wizard_picks_first_disputable(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": _three_eligible_jobs(), "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "1\nthree word reason here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["job_id"] == "job-A"


def test_wizard_picks_third_disputable(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": _three_eligible_jobs(), "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "3\nthree word reason here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["job_id"] == "job-C"


def test_wizard_skips_ineligible_in_numbering(monkeypatch) -> None:
    """`2` should select the second *eligible* row, not the second display row."""
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [
                make_job(job_id="job-A"),
                make_ineligible_job(code="dispute.already_rated", job_id="job-mid"),
                make_job(job_id="job-C"),
            ],
            "next_cursor": None,
        }
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "2\nthree word reason here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["job_id"] == "job-C"


def test_wizard_rejects_non_numeric_then_succeeds(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": _three_eligible_jobs(), "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "abc\n1\nthree word reason here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert "Enter a number" in result.output
    assert fake.dispute_calls[0]["job_id"] == "job-A"


def test_wizard_rejects_out_of_range_then_succeeds(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": _three_eligible_jobs(), "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "99\n1\nthree word reason here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert "between 1 and 3" in result.output
    assert fake.dispute_calls[0]["job_id"] == "job-A"


def test_wizard_default_pick_is_1(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": _three_eligible_jobs(), "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "\nthree word reason here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["job_id"] == "job-A"


# ---------------------------------------------------------------------------
# Reason / evidence prompts
# ---------------------------------------------------------------------------


def test_wizard_reason_required(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "y\n\nthree word reason here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert "Reason is required" in result.output
    assert fake.dispute_calls[0]["reason"] == "three word reason here"


def test_wizard_reason_too_short_rejected(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "y\nbad\nthree good words here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert "at least three words" in result.output
    assert fake.dispute_calls[0]["reason"] == "three good words here"


def test_wizard_evidence_optional_skipped(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "y\nthree word reason here\n\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["evidence"] is None


def test_wizard_evidence_passed_when_given(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "y\nthree word reason here\nhttps://example.com\ny\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert fake.dispute_calls[0]["evidence"] == "https://example.com"


# ---------------------------------------------------------------------------
# Confirmation prompt
# ---------------------------------------------------------------------------


def test_wizard_decline_at_final_confirm(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    stdin = "y\nthree word reason here\n\nn\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, result.output
    assert "Cancelled" in result.output
    assert fake.dispute_calls == []


# ---------------------------------------------------------------------------
# Picker rendering
# ---------------------------------------------------------------------------


def test_wizard_picker_shows_relative_time(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [
                make_job(
                    completed_at=datetime.now(timezone.utc) - timedelta(minutes=5)
                )
            ],
            "next_cursor": None,
        }
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    assert "5m ago" in result.output


def test_wizard_picker_shows_price_in_dollars(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(price_cents=250)], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    assert "$ 2.50" in result.output or "$2.50" in result.output


def test_wizard_picker_shows_input_summary_truncated(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [make_job(input_payload={"task": "x" * 200})],
            "next_cursor": None,
        }
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    # Truncation marker on the input summary line.
    assert "…" in result.output


def test_wizard_picker_labels_caller_vs_worker(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [
                make_job(job_id="ja", caller_owner_id="owner-self"),
                make_job(job_id="jb", caller_owner_id="someone-else"),
            ],
            "next_cursor": None,
        }
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    assert "as caller" in result.output
    assert "as worker" in result.output


def test_wizard_picker_uses_status_filter(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    runner.invoke(app, ["dispute"], input="n\n")
    assert fake.list_jobs_calls
    assert fake.list_jobs_calls[0]["status"] == "complete,failed"


def test_wizard_limit_flag_passed_to_list_jobs(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    runner.invoke(app, ["dispute", "--limit", "5"], input="n\n")
    assert fake.list_jobs_calls[0]["limit"] == 5


# ---------------------------------------------------------------------------
# Authentication / settings
# ---------------------------------------------------------------------------


def test_wizard_requires_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZTEA_API_KEY", raising=False)
    monkeypatch.setenv("AZTEA_CONFIG_DIR", str(tmp_path / "empty"))
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    assert result.exit_code == 1
    assert "aztea login" in result.output


def test_wizard_greeting_shows_base_url(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    monkeypatch.setenv("AZTEA_BASE_URL", "http://localhost:9999")
    result = runner.invoke(app, ["dispute"], input="n\n")
    assert "http://localhost:9999" in result.output
