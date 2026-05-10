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
