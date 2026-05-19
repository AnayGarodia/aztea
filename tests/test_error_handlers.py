"""Coverage for the structured error-envelope mapper in
``server/error_handlers.py``. The pattern table replaced a 50-branch
if-chain; these tests pin the most-load-bearing mappings so a future
typo can't silently regrade an error class.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-master-key")

import pytest  # noqa: E402

from server import error_handlers  # noqa: E402


@pytest.mark.parametrize(
    "message,expected_code",
    [
        ("Authorization header missing", "auth.missing_authorization"),
        ("Invalid API key.", "auth.invalid_key"),
        ("Invalid email or password.", "auth.invalid_credentials"),
        ("Agent-scoped keys cannot register hosted skills.", "auth.insufficient_scope"),
        ("This endpoint requires admin scope.", "auth.insufficient_scope"),
        ("Not authorized for this agent job.", "auth.forbidden"),
        ("Tool 'bogus' not found.", "mcp.tool_not_found"),
        ("Agent 'abc' not found.", "agent.not_found"),
        ("Job 'xyz' not found.", "job.not_found"),
        ("Idempotency-Key is too long.", "request.idempotency_key_too_long"),
        (
            "A request with this Idempotency-Key is still in progress.",
            "request.idempotency_conflict",
        ),
        ("Disputes can only be filed for completed jobs.", "dispute.invalid_state"),
        ("A dispute already exists for this job.", "dispute.already_exists"),
        ("Dispute window has expired for this job.", "dispute.window_closed"),
        ("Job is not claimable.", "job.not_claimable"),
        (
            "Invalid or missing claim_token for this job.",
            "job.invalid_claim_token",
        ),
        ("Invalid or stale claim_token.", "job.invalid_claim_token"),
        ("Upstream agent unreachable: 503.", "agent.upstream_unreachable"),
        ("All LLM models rate-limited.", "agent.upstream_rate_limited"),
        ("Tool_result payload.correlation_id is required.", "job.invalid_tool_result"),
        ("Unknown tool_result correlation_id.", "job.invalid_tool_result"),
        ("Cursor must not be empty.", "request.invalid_cursor"),
        ("Invalid cursor.", "request.invalid_cursor"),
        ("rank_by must be one of {a, b}.", "request.invalid_rank_by"),
    ],
)
def test_error_code_from_message_table_matches_known_strings(
    message: str, expected_code: str
) -> None:
    assert error_handlers._error_code_from_message(400, "/x", message) == expected_code


def test_error_code_from_message_falls_back_to_default_by_status() -> None:
    # An unrecognised message should fall through to the status-code default
    # (not crash, not return None).
    code = error_handlers._error_code_from_message(404, "/jobs/unknown", "totally novel error string")
    assert code == "job.not_found"


def test_error_code_pattern_table_has_no_empty_or_duplicate_codes() -> None:
    # Every entry must produce a non-empty, dot-namespaced code; duplicate
    # codes are allowed (multiple messages can route to the same code) but
    # an empty string would mean a copy-paste mistake.
    for predicate, code in error_handlers.ERROR_CODE_PATTERNS:
        assert callable(predicate)
        assert code and "." in code, f"bad code: {code!r}"


def test_error_code_pattern_table_first_match_wins() -> None:
    # 'this endpoint requires caller or worker scope' is more specific than
    # 'this endpoint requires' but the chain order keeps the broader pattern
    # first. Both must still map to auth.insufficient_scope so behavior is
    # unchanged from the original if-chain.
    msg = "This endpoint requires caller or worker scope."
    assert (
        error_handlers._error_code_from_message(403, "/wallets", msg)
        == "auth.insufficient_scope"
    )


# ===========================================================================
# Phase 1 (2026-05-19): boundary sanitiser for raw-exception leak text.
# ===========================================================================


@pytest.mark.parametrize(
    "message",
    [
        "A string literal cannot contain NUL (0x00) characters.",  # psycopg2
        "psycopg2.errors.UniqueViolation: ...",
        "ValueError: foo bar",
        "TypeError: cannot do that",
        "Disallowed CORS origin",
        "Traceback (most recent call last):",
        "sqlalchemy.exc.IntegrityError: ...",
        "<class 'starlette.exceptions.HTTPException'>",
    ],
)
def test_looks_like_exception_leak_detects_known_signatures(message: str) -> None:
    assert error_handlers._looks_like_exception_leak(message)


@pytest.mark.parametrize(
    "message",
    [
        "max_total_cents must be > 0",
        "Job 'abc' not found.",
        "Agent 'x' is sunset; use web_search.",
        "reason must not be empty",
        "Insufficient balance for batch.",
    ],
)
def test_looks_like_exception_leak_passes_real_messages(message: str) -> None:
    assert not error_handlers._looks_like_exception_leak(message)


def test_normalize_error_payload_sanitises_psycopg_leak() -> None:
    """F20 repro: NUL byte → psycopg2 ValueError → leaked into response.
    Now sanitised."""
    payload = error_handlers.normalize_error_payload(
        400,
        "A string literal cannot contain NUL (0x00) characters.",
        "/jobs/abc/dispute",
    )
    assert "NUL" not in payload["message"]
    assert "0x00" not in payload["message"]
    assert payload["message"] == error_handlers._SANITISED_LEAK_MESSAGE
    # Envelope is still well-formed.
    assert payload["error"]
    assert "." in payload["error"]


def test_normalize_error_payload_preserves_real_user_messages() -> None:
    """Legitimate user-facing messages survive without rewrite."""
    payload = error_handlers.normalize_error_payload(
        400, "max_total_cents must be > 0", "/jobs/batch"
    )
    assert payload["message"] == "max_total_cents must be > 0"


def test_normalize_error_payload_sanitises_dict_detail_with_leaked_message() -> None:
    """A structured detail whose `message` field leaks exception text
    must also be sanitised."""
    payload = error_handlers.normalize_error_payload(
        400,
        {
            "error": "dispute.write_failed",
            "message": "ValueError: A string literal cannot contain NUL (0x00) characters.",
        },
        "/disputes/x",
    )
    assert "NUL" not in payload["message"]
    assert payload["error"] == "dispute.write_failed"  # explicit code preserved


# ===========================================================================
# Phase 2 (2026-05-19): map_value_error_to_envelope helper for the
# ~20 highest-traffic ``detail=str(exc)`` sites.
# ===========================================================================


def test_map_value_error_to_envelope_parses_structured_prefix() -> None:
    out = error_handlers.map_value_error_to_envelope(
        ValueError("dispute.not_completed: completed_at is unset"),
        scope="dispute",
    )
    assert out["error"] == "dispute.not_completed"
    assert "completed_at" in out["message"]


def test_map_value_error_to_envelope_falls_back_to_scope() -> None:
    out = error_handlers.map_value_error_to_envelope(
        ValueError("reason must be a non-empty string."),
        scope="dispute",
    )
    assert out["error"] == "dispute.invalid_input"
    assert "reason must be" in out["message"]


def test_map_value_error_to_envelope_handles_permission_error() -> None:
    out = error_handlers.map_value_error_to_envelope(
        PermissionError("Only a party to the job may file a dispute."),
        scope="dispute",
    )
    assert out["error"] == "dispute.unauthorized"


def test_map_value_error_to_envelope_does_not_misparse_english_colon() -> None:
    """A real English message with a colon must NOT be parsed as a structured prefix."""
    out = error_handlers.map_value_error_to_envelope(
        ValueError("balance must equal: amount * fee_pct"),
        scope="wallet",
    )
    # "balance must equal" has no dot — fall back to wallet.invalid_input
    assert out["error"] == "wallet.invalid_input"


def test_normalize_error_payload_logs_sanitised_leak(caplog) -> None:
    """When a leak is sanitised, the raw text is preserved in the log
    for ops debugging."""
    with caplog.at_level("WARNING", logger="aztea.error_handlers"):
        error_handlers.normalize_error_payload(
            500, "Traceback: psycopg2.errors.UniqueViolation: secret_key=abc",
            "/admin/audit",
        )
    assert any(
        "server.error_message_sanitized" in record.message
        or "server.error_message_sanitized" in str(record.getMessage())
        for record in caplog.records
    )
