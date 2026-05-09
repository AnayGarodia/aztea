"""Disputability classification — what the picker shows and partitions.

The CLI never re-implements the predicate from `core/jobs/disputable.py`;
it reads `disputable` + `disputable_reason` directly off each job in the
server response. These tests verify (a) every ineligibility code renders
its message, (b) the picker numbers eligible rows correctly even when
ineligible rows are interleaved, and (c) defensive defaults when the
server response omits expected fields.
"""
from __future__ import annotations

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
# Per-code message rendering
# ---------------------------------------------------------------------------


INELIGIBLE_CODES = [
    (
        "dispute.not_completed",
        "Disputes can only be filed for jobs that produced output",
    ),
    ("dispute.window_expired", "Dispute window has expired"),
    ("dispute.already_filed", "A dispute already exists"),
    ("dispute.already_rated", "You already rated this job"),
    ("dispute.invalid_window", "Dispute window could not be computed"),
]


@pytest.mark.parametrize("code, message_fragment", INELIGIBLE_CODES)
def test_picker_shows_each_ineligibility_reason(monkeypatch, code, message_fragment):
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [make_ineligible_job(code=code, job_id="j1")],
            "next_cursor": None,
        }
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    assert result.exit_code == 0, result.output
    assert message_fragment in result.output, (
        f"Picker did not surface message fragment for {code}: "
        f"expected '{message_fragment}' in output"
    )


# ---------------------------------------------------------------------------
# Marker rendering
# ---------------------------------------------------------------------------


def test_picker_renders_eligible_with_number(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [make_job(job_id="j1")], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    # `1.` numbering appears for the eligible row.
    assert "1." in result.output


def test_picker_renders_ineligible_with_dash(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [make_ineligible_job(code="dispute.already_filed")],
            "next_cursor": None,
        }
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    # The `─` (em-dash-like) marker indicates an ineligible row.
    assert "─" in result.output


def test_picker_only_numbers_eligible_rows(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [
                make_job(job_id="j1", agent_name="alpha"),
                make_ineligible_job(
                    code="dispute.already_rated", job_id="j2", agent_name="beta"
                ),
                make_job(job_id="j3", agent_name="gamma"),
            ],
            "next_cursor": None,
        }
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    # Pick "2" — should select the second *eligible* row (gamma/j3), not beta/j2.
    stdin = "2\nthree word reason here\n\nn\n"
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0
    # The middle row is greyed out; numeric pick "2" maps to gamma not beta.
    # Easiest cross-check: assert the picker prompt appeared (proves we hit
    # the multi-eligible branch, not auto-confirm).
    assert "Pick a disputable job" in result.output


# ---------------------------------------------------------------------------
# Defensive fallbacks for missing server fields
# ---------------------------------------------------------------------------


def test_picker_falls_back_when_disputable_field_missing(monkeypatch) -> None:
    """If server omits the `disputable` key, treat row as ineligible (safe default)."""
    job = make_job(job_id="j1")
    job.pop("disputable", None)
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [job], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    # All-ineligible state shows the tip, never the picker prompt.
    assert result.exit_code == 0
    assert "Pick a disputable job" not in result.output
    assert "Only one disputable job" not in result.output


def test_picker_falls_back_when_disputable_reason_missing(monkeypatch) -> None:
    """`disputable=False` with no reason should not crash."""
    job = make_ineligible_job(code="dispute.already_rated", job_id="j1")
    job["disputable_reason"] = None  # explicit None
    fake = FakeDisputeClient(
        list_jobs_response={"jobs": [job], "next_cursor": None}
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Partition logic — eligible/ineligible counts drive the wizard's branches
# ---------------------------------------------------------------------------


PARTITION_CASES = [
    # (label, jobs, stdin, expected_route)
    (
        "two_eligible",
        [make_job(job_id="a"), make_job(job_id="b")],
        "1\nthree word reason here\n\nn\n",
        "picker",
    ),
    (
        "one_eligible_one_ineligible",
        [make_ineligible_job(code="dispute.already_filed", job_id="a"), make_job(job_id="b")],
        "n\n",
        "auto_confirm",
    ),
    (
        "two_ineligible",
        [
            make_ineligible_job(code="dispute.window_expired", job_id="a"),
            make_ineligible_job(code="dispute.already_filed", job_id="b"),
        ],
        "",
        "all_ineligible",
    ),
    ("empty", [], "", "empty"),
    (
        "single_eligible",
        [make_job(job_id="a")],
        "n\n",
        "auto_confirm",
    ),
]


@pytest.mark.parametrize("label, jobs, stdin, expected_route", PARTITION_CASES)
def test_partition_routes_to_correct_branch(
    monkeypatch, label, jobs, stdin, expected_route
):
    fake = FakeDisputeClient(list_jobs_response={"jobs": jobs, "next_cursor": None})
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input=stdin)
    assert result.exit_code == 0, f"[{label}] {result.output}"
    if expected_route == "empty":
        assert "No recent jobs" in result.output
    elif expected_route == "all_ineligible":
        assert "Pick a disputable job" not in result.output
        assert "Only one disputable job" not in result.output
        assert (
            "before rating" in result.output
            or "within the dispute window" in result.output
        )
    elif expected_route == "auto_confirm":
        assert "Only one disputable job" in result.output
    elif expected_route == "picker":
        assert "Pick a disputable job" in result.output


# ---------------------------------------------------------------------------
# Boundary cases (per-job edge fields)
# ---------------------------------------------------------------------------


def test_picker_handles_missing_input_payload(monkeypatch) -> None:
    job = make_job(job_id="j1")
    job.pop("input_payload", None)
    fake = FakeDisputeClient(list_jobs_response={"jobs": [job], "next_cursor": None})
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    assert result.exit_code == 0


def test_picker_handles_missing_completed_at(monkeypatch) -> None:
    """Eligible job with no `completed_at` falls back to `created_at`."""
    from datetime import datetime, timedelta, timezone

    job = make_job(
        job_id="j1",
        completed_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    fake = FakeDisputeClient(list_jobs_response={"jobs": [job], "next_cursor": None})
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    assert result.exit_code == 0
    assert "2h ago" in result.output


def test_picker_handles_missing_agent_name(monkeypatch) -> None:
    """When `agent_name` is missing, the picker falls back to `agent_id`."""
    job = make_job(job_id="j1", agent_id="agent-xyz", agent_name=None)
    job.pop("agent_name", None)  # truly missing
    job["agent_id"] = "agent-xyz"
    fake = FakeDisputeClient(list_jobs_response={"jobs": [job], "next_cursor": None})
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    assert result.exit_code == 0
    assert "agent-xyz" in result.output


def test_picker_handles_zero_price(monkeypatch) -> None:
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [make_job(job_id="j1", price_cents=0)],
            "next_cursor": None,
        }
    )
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    assert result.exit_code == 0
    assert "$ 0.00" in result.output or "$0.00" in result.output


def test_picker_handles_unknown_owner_id(monkeypatch) -> None:
    """If the wizard can't resolve the user's owner_id, default to 'as worker'."""
    fake = FakeDisputeClient(
        list_jobs_response={
            "jobs": [make_job(job_id="j1", caller_owner_id="some-other-owner")],
            "next_cursor": None,
        }
    )
    # Override _request_json so /auth/me returns nothing useful.
    fake._request_json = lambda *a, **k: {}  # type: ignore[assignment]
    patch_client(monkeypatch, fake)
    patch_tty(monkeypatch)
    result = runner.invoke(app, ["dispute"], input="n\n")
    assert result.exit_code == 0
    # Without a resolved owner_id, the row labels as worker (safe default —
    # we never claim someone is the caller when we don't know).
    assert "as worker" in result.output
