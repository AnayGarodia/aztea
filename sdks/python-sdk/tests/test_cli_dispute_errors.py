"""Error mapping for `aztea dispute` — every code in `_DISPUTE_ERROR_HINTS`,
the `InsufficientBalance` shortfall math, fallthrough cases, and network
failure modes."""
from __future__ import annotations

import pytest
import requests
from typer.testing import CliRunner

from aztea.cli import app
from aztea.cli.dispute import _DISPUTE_ERROR_HINTS
from aztea.errors import (
    APIError,
    AuthenticationError,
    ConflictError,
    InsufficientBalanceError,
    NotFoundError,
    RateLimitError,
)

from ._dispute_helpers import FakeDisputeClient, patch_client


runner = CliRunner()


# ---------------------------------------------------------------------------
# Per-code mapping (parameterized over every entry in _DISPUTE_ERROR_HINTS)
# ---------------------------------------------------------------------------


HINT_FRAGMENTS = [
    ("dispute.window_expired",                       "window for this job has closed"),
    ("dispute.already_filed",                        "aztea dispute --status"),
    ("dispute.already_rated",                        "before a quality rating"),
    ("dispute.not_completed",                        "hasn't finished yet"),
    ("dispute.invalid_window",                       "Contact support"),
    ("dispute.filing_deposit_insufficient_balance",  "Top up"),
    ("dispute.clawback_insufficient_balance",        "couldn't be locked"),
    ("job.self_dispute_not_allowed",                 "agent you own"),
]


def test_every_dispute_error_hint_has_a_test() -> None:
    """Drift guard: every entry in _DISPUTE_ERROR_HINTS must have a row above."""
    covered = {code for code, _ in HINT_FRAGMENTS}
    declared = set(_DISPUTE_ERROR_HINTS.keys())
    missing = declared - covered
    assert not missing, f"_DISPUTE_ERROR_HINTS has untested codes: {sorted(missing)}"


@pytest.mark.parametrize("code, hint_fragment", HINT_FRAGMENTS)
def test_dispute_error_code_renders_specific_hint(monkeypatch, code, hint_fragment):
    err = APIError(
        status_code=400,
        message=f"Server message for {code}",
        detail=None,
        body=None,
        code=code,
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    assert hint_fragment in result.output, (
        f"For code={code}: expected hint fragment '{hint_fragment}' in output. "
        f"Got: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# InsufficientBalanceError shortfall math
# ---------------------------------------------------------------------------


def test_insufficient_balance_renders_exact_topup_amount(monkeypatch) -> None:
    err = InsufficientBalanceError(
        status_code=409,
        message="not enough",
        detail={
            "error": "dispute.filing_deposit_insufficient_balance",
            "balance_cents": 3,
            "required_cents": 10,
        },
        body=None,
        code="dispute.filing_deposit_insufficient_balance",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    assert "$0.07" in result.output
    assert "aztea wallet topup" in result.output


def test_insufficient_balance_zero_shortfall_falls_back(monkeypatch) -> None:
    err = InsufficientBalanceError(
        status_code=409,
        message="exact",
        detail={
            "error": "dispute.filing_deposit_insufficient_balance",
            "balance_cents": 10,
            "required_cents": 10,
        },
        body=None,
        code="dispute.filing_deposit_insufficient_balance",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    # Falls back to the generic top-up hint when shortfall is 0.
    assert "Top up" in result.output


def test_insufficient_balance_handles_missing_amounts(monkeypatch) -> None:
    err = InsufficientBalanceError(
        status_code=409,
        message="missing fields",
        detail={"error": "dispute.filing_deposit_insufficient_balance"},
        body=None,
        code="dispute.filing_deposit_insufficient_balance",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    # Still renders cleanly, no traceback.
    assert "Traceback" not in result.output


def test_insufficient_balance_clawback_phase_renders_correct_code(monkeypatch) -> None:
    err = InsufficientBalanceError(
        status_code=409,
        message="clawback failed",
        detail={
            "error": "dispute.clawback_insufficient_balance",
            "balance_cents": 0,
            "required_cents": 100,
        },
        body=None,
        code="dispute.clawback_insufficient_balance",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    assert "couldn't be locked" in result.output


# ---------------------------------------------------------------------------
# Fallthrough — unknown codes / generic errors
# ---------------------------------------------------------------------------


def test_unknown_error_code_falls_through_to_generic_handler(monkeypatch) -> None:
    err = APIError(
        status_code=500,
        message="something else",
        detail=None,
        body=None,
        code="some.unknown.code",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    # Hint fragments for known codes must NOT appear.
    for _, fragment in HINT_FRAGMENTS:
        if fragment in ("Top up",):  # generic enough to false-match elsewhere
            continue
        assert fragment not in result.output, (
            f"Generic fallthrough leaked dispute-specific hint '{fragment}'"
        )


def test_authentication_error_routes_to_generic_handler(monkeypatch) -> None:
    err = AuthenticationError(
        status_code=401,
        message="bad creds",
        detail=None,
        body=None,
        code="auth.invalid",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    assert "aztea login" in result.output


def test_rate_limit_error_routes_to_generic_handler(monkeypatch) -> None:
    err = RateLimitError(
        status_code=429,
        message="slow down",
        detail=None,
        body=None,
        code="rate_limit",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    assert "Wait" in result.output or "retry" in result.output.lower()


def test_not_found_error_routes_cleanly(monkeypatch) -> None:
    err = NotFoundError(
        status_code=404,
        message="job not found",
        detail=None,
        body=None,
        code="not_found",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-bogus", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# Network / transport failures
# ---------------------------------------------------------------------------


def test_network_timeout_during_filing_surfaces_cleanly(monkeypatch) -> None:
    fake = FakeDisputeClient(dispute_raises=requests.Timeout("timed out"))
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_connection_error_during_filing_surfaces_cleanly(monkeypatch) -> None:
    fake = FakeDisputeClient(
        dispute_raises=requests.ConnectionError("refused")
    )
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code != 0


def test_unexpected_500_during_filing_surfaces_cleanly(monkeypatch) -> None:
    err = APIError(
        status_code=500,
        message="internal",
        detail=None,
        body=None,
        code="DISPUTE_FILING_FAILED",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Error envelope edge cases
# ---------------------------------------------------------------------------


def test_error_with_nested_detail_dict_extracts_code(monkeypatch) -> None:
    """FastAPI HTTPException wraps the body in `{"detail": {...}}`. Make sure
    the CLI peels that off when extracting the structured `error` code."""
    err = ConflictError(
        status_code=409,
        message="wrapped",
        detail={"detail": {"error": "dispute.window_expired"}},
        body=None,
        code=None,  # only nested error has the code
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    assert "window for this job has closed" in result.output


def test_error_with_string_detail_falls_through(monkeypatch) -> None:
    err = APIError(
        status_code=400,
        message="some error",
        detail="some string detail",
        body=None,
        code=None,
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1


def test_error_with_none_detail_falls_through(monkeypatch) -> None:
    err = APIError(
        status_code=400,
        message="empty",
        detail=None,
        body=None,
        code=None,
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1


def test_self_dispute_error_blocks_with_friendly_hint(monkeypatch) -> None:
    err = APIError(
        status_code=400,
        message="self dispute",
        detail=None,
        body=None,
        code="job.self_dispute_not_allowed",
    )
    fake = FakeDisputeClient(dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(
        app, ["dispute", "job-1", "--reason", "test", "--yes"]
    )
    assert result.exit_code == 1
    assert "agent you own" in result.output


# ---------------------------------------------------------------------------
# Status mode error handling
# ---------------------------------------------------------------------------


def test_status_mode_404_renders_cleanly(monkeypatch) -> None:
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
    assert "Traceback" not in result.output


def test_status_mode_authentication_error(monkeypatch) -> None:
    err = AuthenticationError(
        status_code=401,
        message="bad creds",
        detail=None,
        body=None,
        code="auth.invalid",
    )
    fake = FakeDisputeClient(get_dispute_raises=err)
    patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["dispute", "--status", "job-1"])
    assert result.exit_code == 1
    assert "aztea login" in result.output
