"""Job messages, models parsing, and migration edge cases."""
from tests.jobs_core_harness import (
    isolated_jobs_db,
    _create_job,
    _get_claim_events,
    _init_jobs_db,
    _latest_message_id,
    _set_claim_events_lease_expiry,
)
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from core import error_codes
from core import jobs
from core import models

def test_clarification_message_flow_preserves_claim_and_extends_lease(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:clarify")

    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:clarify", lease_seconds=60)
    assert claimed is not None
    claim_token = claimed["claim_token"]
    first_lease_expiry = datetime.fromisoformat(claimed["lease_expires_at"])

    asked = jobs.add_message(
        job["job_id"],
        from_id="worker:clarify",
        msg_type="clarification_needed",
        payload={"question": "Need additional context."},
        lease_seconds=120,
    )
    assert asked is not None

    awaiting = jobs.get_job(job["job_id"])
    assert awaiting is not None
    assert awaiting["status"] == "awaiting_clarification"
    assert awaiting["claim_owner_id"] == "worker:clarify"
    assert awaiting["claim_token"] == claim_token
    assert datetime.fromisoformat(awaiting["lease_expires_at"]) > first_lease_expiry

    first_extension_expiry = datetime.fromisoformat(awaiting["lease_expires_at"])

    answered = jobs.add_message(
        job["job_id"],
        from_id=job["caller_owner_id"],
        msg_type="clarification",
        payload={"answer": "Use fiscal-year totals."},
        lease_seconds=90,
    )
    assert answered is not None
    assert answered["message_id"] > asked["message_id"]

    resumed = jobs.get_job(job["job_id"])
    assert resumed is not None
    assert resumed["status"] == "running"
    assert resumed["claim_owner_id"] == "worker:clarify"
    assert resumed["claim_token"] == claim_token
    assert datetime.fromisoformat(resumed["lease_expires_at"]) > first_extension_expiry

    all_messages = jobs.get_messages(job["job_id"])
    human_messages = [item for item in all_messages if item["type"] != "claim_event"]
    claim_events = _get_claim_events(job["job_id"])

    assert [item["type"] for item in human_messages] == ["clarification_needed", "clarification"]
    assert _latest_message_id(job["job_id"]) == all_messages[-1]["message_id"]
    assert [item["message_id"] for item in jobs.get_messages(job["job_id"], since_id=asked["message_id"])] == [
        item["message_id"] for item in all_messages if item["message_id"] > asked["message_id"]
    ]

    event_types = [event.get("event_type") for event in claim_events]
    assert "claim_acquired" in event_types
    assert event_types.count("claim_lease_extended") >= 2


def test_claim_token_recent_activity_helper_respects_grace_window(isolated_jobs_db):
    _init_jobs_db()
    if not hasattr(jobs, "claim_token_was_recently_active"):
        pytest.skip("claim_token_was_recently_active helper is not available in this core build.")
    job = _create_job(agent_owner_id="worker:grace")

    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:grace", lease_seconds=45)
    assert claimed is not None
    claim_token = claimed["claim_token"]
    claim_owner_id = claimed["claim_owner_id"]

    assert jobs.claim_token_was_recently_active(
        job["job_id"],
        claim_owner_id=claim_owner_id,
        claim_token=claim_token,
        within_seconds=60,
    )

    stale_lease_expires_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _set_claim_events_lease_expiry(job["job_id"], stale_lease_expires_at)

    assert not jobs.claim_token_was_recently_active(
        job["job_id"],
        claim_owner_id=claim_owner_id,
        claim_token=claim_token,
        within_seconds=60,
    )

    refreshed = jobs.heartbeat_job_lease(
        job["job_id"],
        claim_owner_id=claim_owner_id,
        claim_token=claim_token,
        lease_seconds=120,
    )
    assert refreshed is not None

    assert jobs.claim_token_was_recently_active(
        job["job_id"],
        claim_owner_id=claim_owner_id,
        claim_token=claim_token,
        within_seconds=60,
    )


def test_lease_helpers_classify_stale_and_unclaimed_jobs(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:helpers")

    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:helpers", lease_seconds=60)
    assert claimed is not None

    now_dt = datetime.now(timezone.utc)
    assert jobs._lease_is_active(claimed, now_dt)
    assert not jobs._lease_is_expired(claimed, now_dt)

    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = NULL, status = 'running' WHERE job_id = ?",
            (job["job_id"],),
        )

    stale = jobs.get_job(job["job_id"])
    assert stale is not None
    assert not jobs._lease_is_active(stale, now_dt)
    assert jobs._lease_is_expired(stale, now_dt)
    assert job["job_id"] in {
        item["job_id"]
        for item in jobs.list_jobs_with_expired_leases(
            now=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        )
    }

    released = jobs.release_job_claim(
        job["job_id"],
        claim_owner_id="worker:helpers",
        claim_token=claimed["claim_token"],
    )
    assert released is not None
    assert not jobs._lease_is_active(released, now_dt)
    assert not jobs._lease_is_expired(released, now_dt)
    assert job["job_id"] not in {item["job_id"] for item in jobs.list_jobs_with_expired_leases()}


def test_terminal_status_updates_are_idempotent_after_completion(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:idempotent")

    first = jobs.update_job_status(
        job["job_id"],
        status="complete",
        output_payload={"ok": True},
        completed=True,
    )
    assert first is not None
    assert first["status"] == "complete"
    assert first["output_payload"] == {"ok": True}
    assert first["completed_at"] is not None

    second = jobs.update_job_status(
        job["job_id"],
        status="failed",
        error_message="late worker write should be ignored",
        completed=True,
    )
    assert second is not None
    assert second["status"] == "complete"
    assert second["output_payload"] == {"ok": True}
    assert second["error_message"] is None
    assert second["completed_at"] == first["completed_at"]


@pytest.mark.parametrize(
    ("body", "expected_type", "expected_correlation"),
    [
        (
            {
                "type": "clarification_request",
                "payload": {"question": " Need context ", "schema": {"fields": ["ticker"]}},
            },
            "clarification_request",
            None,
        ),
        (
            {
                "type": "clarification_response",
                "payload": {"answer": " Use GAAP totals ", "request_message_id": 7},
            },
            "clarification_response",
            None,
        ),
        (
            {
                "type": "progress",
                "payload": {"percent": 55, "note": " halfway "},
            },
            "progress",
            None,
        ),
        (
            {
                "type": "partial_result",
                "payload": {"payload": {"rows": 2}, "is_final": False},
            },
            "partial_result",
            None,
        ),
        (
            {
                "type": "artifact",
                "payload": {
                    "name": "brief.json",
                    "mime": "application/json",
                    "url_or_base64": "https://example.test/brief.json",
                    "size_bytes": 12,
                },
            },
            "artifact",
            None,
        ),
        (
            {
                "type": "tool_call",
                "payload": {
                    "tool_name": "search",
                    "args": {"ticker": "AAPL"},
                    "correlation_id": "corr-tool-call",
                },
            },
            "tool_call",
            "corr-tool-call",
        ),
        (
            {
                "type": "tool_result",
                "correlation_id": "corr-tool-result",
                "payload": {
                    "correlation_id": "corr-tool-result",
                    "payload": {"ok": True},
                    "error": " ",
                },
            },
            "tool_result",
            "corr-tool-result",
        ),
        (
            {
                "type": "note",
                "payload": {"text": "  worker note  "},
            },
            "note",
            None,
        ),
        (
            {
                "type": "agent_message",
                "payload": {
                    "channel": "rendering",
                    "body": {"request": "generate-stl"},
                    "to_id": "agent:cad-specialist",
                },
            },
            "agent_message",
            None,
        ),
    ],
)
def test_parse_typed_job_message_accepts_all_supported_types(
    body: dict,
    expected_type: str,
    expected_correlation: str | None,
):
    parsed = models.parse_typed_job_message(body)
    normalized = parsed.model_dump()
    assert normalized["type"] == expected_type
    assert normalized.get("correlation_id") == expected_correlation


@pytest.mark.parametrize(
    "body",
    [
        {"type": "clarification_request", "payload": {"question": " "}},
        {"type": "clarification_response", "payload": {"answer": "ok"}},
        {"type": "progress", "payload": {"percent": 101}},
        {"type": "partial_result", "payload": {"payload": {}, "is_final": True}},
        {
            "type": "artifact",
            "payload": {"name": "n", "mime": "m", "url_or_base64": "u", "size_bytes": -1},
        },
        {"type": "agent_message", "payload": {"channel": " ", "body": {"ok": True}}},
        {"type": "tool_call", "payload": {"tool_name": " ", "args": {}}},
        {"type": "tool_result", "payload": {"payload": {"ok": True}}},
        {"type": "note", "payload": {"text": " "}},
    ],
)
def test_parse_typed_job_message_rejects_invalid_payloads(body: dict):
    with pytest.raises(ValidationError):
        models.parse_typed_job_message(body)


def test_normalize_job_message_body_supports_legacy_clarification_types():
    asked = models.normalize_job_message_body(
        msg_type="clarification_needed",
        payload={"question": "Need totals by segment."},
        allow_legacy=True,
    )
    assert asked["type"] == "clarification_needed"
    assert asked["canonical_type"] == "clarification_request"

    answered = models.normalize_job_message_body(
        msg_type="clarification",
        payload={"answer": "Use fiscal-year totals."},
        allow_legacy=True,
    )
    assert answered["type"] == "clarification"
    assert answered["canonical_type"] == "clarification_response"
    assert answered["payload"]["answer"] == "Use fiscal-year totals."


def test_clarification_request_message_marks_awaiting_and_extends_lease(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:typed-clarification-request")
    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:typed-clarification-request", lease_seconds=45)
    assert claimed is not None
    previous_expiry = datetime.fromisoformat(claimed["lease_expires_at"])

    message = jobs.add_message(
        job["job_id"],
        from_id="worker:typed-clarification-request",
        msg_type="clarification_request",
        payload={"question": "Need calendarized revenue and schema.", "schema": {"required": ["answer"]}},
        lease_seconds=120,
    )
    assert message is not None
    assert message["type"] == "clarification_request"

    updated = jobs.get_job(job["job_id"])
    assert updated is not None
    assert updated["status"] == "awaiting_clarification"
    assert datetime.fromisoformat(updated["lease_expires_at"]) > previous_expiry


def test_clarification_response_message_resumes_running_and_extends_lease(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:typed-clarification-response")
    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:typed-clarification-response", lease_seconds=45)
    assert claimed is not None

    asked = jobs.add_message(
        job["job_id"],
        from_id="worker:typed-clarification-response",
        msg_type="clarification_request",
        payload={"question": "Please provide region split."},
        lease_seconds=60,
    )
    awaiting = jobs.get_job(job["job_id"])
    assert awaiting is not None
    assert awaiting["status"] == "awaiting_clarification"
    previous_expiry = datetime.fromisoformat(awaiting["lease_expires_at"])

    message = jobs.add_message(
        job["job_id"],
        from_id=job["caller_owner_id"],
        msg_type="clarification_response",
        payload={"answer": {"region": "NA"}, "request_message_id": asked["message_id"]},
        lease_seconds=90,
    )
    assert message is not None
    assert message["type"] == "clarification_response"
    assert message["payload"]["request_message_id"] == asked["message_id"]

    resumed = jobs.get_job(job["job_id"])
    assert resumed is not None
    assert resumed["status"] == "running"
    assert datetime.fromisoformat(resumed["lease_expires_at"]) > previous_expiry


@pytest.mark.parametrize(
    ("msg_type", "payload", "correlation_id", "seed_tool_call"),
    [
        ("progress", {"percent": 25, "note": "working"}, None, False),
        ("partial_result", {"payload": {"summary": "part"}, "is_final": False}, None, False),
        (
            "artifact",
            {
                "name": "memo.txt",
                "mime": "text/plain",
                "url_or_base64": "VGhpcyBpcyBhIG1lbW8=",
                "size_bytes": 16,
            },
            None,
            False,
        ),
        (
            "tool_call",
            {"tool_name": "sec_lookup", "args": {"ticker": "AAPL"}, "correlation_id": "corr-typed-tool"},
            "corr-typed-tool",
            False,
        ),
        (
            "tool_result",
            {"correlation_id": "corr-typed-tool", "payload": {"status": "ok"}, "error": None},
            "corr-typed-tool",
            True,
        ),
        ("note", {"text": "worker checkpoint"}, None, False),
    ],
)
def test_extend_only_message_types_extend_lease_without_status_transition(
    isolated_jobs_db,
    msg_type: str,
    payload: dict,
    correlation_id: str | None,
    seed_tool_call: bool,
):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:typed-extend")
    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:typed-extend", lease_seconds=45)
    assert claimed is not None

    if seed_tool_call:
        jobs.add_message(
            job["job_id"],
            from_id="worker:typed-extend",
            msg_type="tool_call",
            payload={"tool_name": "sec_lookup", "args": {"ticker": "AAPL"}, "correlation_id": "corr-typed-tool"},
            lease_seconds=30,
        )

    baseline = jobs.get_job(job["job_id"])
    assert baseline is not None
    baseline_expiry = datetime.fromisoformat(baseline["lease_expires_at"])

    message = jobs.add_message(
        job["job_id"],
        from_id="worker:typed-extend",
        msg_type=msg_type,
        payload=payload,
        lease_seconds=75,
        correlation_id=correlation_id,
    )
    assert message is not None
    assert message["type"] == msg_type

    updated = jobs.get_job(job["job_id"])
    assert updated is not None
    assert updated["status"] == "running"
    assert datetime.fromisoformat(updated["lease_expires_at"]) > baseline_expiry


def test_tool_call_correlation_helpers_and_tool_result_reference_checks(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:corr")
    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:corr", lease_seconds=45)
    assert claimed is not None

    assert not jobs.tool_call_correlation_exists(job["job_id"], "corr-missing")
    assert not jobs.message_correlation_exists(job["job_id"], "corr-missing")

    with pytest.raises(ValueError, match="no matching tool_call"):
        jobs.add_message(
            job["job_id"],
            from_id="worker:corr",
            msg_type="tool_result",
            payload={"correlation_id": "corr-1", "payload": {"ok": False}, "error": None},
            lease_seconds=45,
        )

    tool_call = jobs.add_message(
        job["job_id"],
        from_id="worker:corr",
        msg_type="tool_call",
        payload={"tool_name": "sec_lookup", "args": {"ticker": "MSFT"}, "correlation_id": "corr-1"},
        lease_seconds=45,
    )
    assert tool_call["correlation_id"] == "corr-1"
    assert jobs.tool_call_correlation_exists(job["job_id"], "corr-1")
    assert jobs.message_correlation_exists(job["job_id"], "corr-1")
    assert jobs.message_correlation_exists(job["job_id"], "corr-1", msg_type="tool_call")

    tool_result = jobs.add_message(
        job["job_id"],
        from_id="worker:corr",
        msg_type="tool_result",
        payload={"correlation_id": "corr-1", "payload": {"ok": True}, "error": None},
        lease_seconds=45,
    )
    assert tool_result["correlation_id"] == "corr-1"
    assert jobs.message_correlation_exists(job["job_id"], "corr-1", msg_type="tool_result")


def test_get_messages_supports_type_sender_and_channel_filters(isolated_jobs_db):
    _init_jobs_db()
    job = _create_job(agent_owner_id="worker:filters")
    claimed = jobs.claim_job(job["job_id"], claim_owner_id="worker:filters", lease_seconds=60)
    assert claimed is not None

    jobs.add_message(
        job["job_id"],
        from_id="worker:filters",
        msg_type="progress",
        payload={"percent": 10, "note": "starting"},
        lease_seconds=60,
    )
    cad_1 = jobs.add_message(
        job["job_id"],
        from_id="worker:filters",
        msg_type="agent_message",
        payload={"channel": "cad", "body": {"phase": "draft"}, "to_id": "agent:cad-specialist"},
        lease_seconds=60,
    )
    jobs.add_message(
        job["job_id"],
        from_id="worker:filters",
        msg_type="agent_message",
        payload={"channel": "video", "body": {"phase": "render"}, "to_id": "agent:video-specialist"},
        lease_seconds=60,
    )
    cad_2 = jobs.add_message(
        job["job_id"],
        from_id="worker:filters",
        msg_type="agent_message",
        payload={"channel": "cad", "body": {"phase": "final"}, "to_id": "agent:cad-specialist"},
        lease_seconds=60,
    )

    filtered = jobs.get_messages(
        job["job_id"],
        msg_type="agent_message",
        from_id="worker:filters",
        channel="cad",
        to_id="agent:cad-specialist",
    )
    assert [item["message_id"] for item in filtered] == [cad_1["message_id"], cad_2["message_id"]]

    since_filtered = jobs.get_messages(
        job["job_id"],
        since_id=cad_1["message_id"],
        msg_type="agent_message",
        channel="cad",
    )
    assert [item["message_id"] for item in since_filtered] == [cad_2["message_id"]]


def test_init_jobs_db_migrates_job_messages_for_correlation_id(isolated_jobs_db):
    with sqlite3.connect(isolated_jobs_db) as conn:
        jobs._create_jobs_table(conn)
        conn.execute(
            """
            CREATE TABLE job_messages (
                message_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id       TEXT NOT NULL,
                from_id      TEXT NOT NULL,
                type         TEXT NOT NULL,
                payload      TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
            """
        )

    _init_jobs_db()

    with sqlite3.connect(isolated_jobs_db) as conn:
        conn.row_factory = sqlite3.Row
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(job_messages)").fetchall()}
        assert "correlation_id" in cols
        indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(job_messages)").fetchall()
        }
        assert "idx_job_messages_job_correlation" in indexes


def test_job_create_request_defaults_verification_window_to_24_hours():
    request = models.JobCreateRequest(agent_id="agent-default", input_payload={"task": "default"})
    assert request.output_verification_window_seconds == 86400


def test_job_create_request_allows_explicit_zero_verification_window():
    request = models.JobCreateRequest(
        agent_id="agent-default",
        input_payload={"task": "explicit-zero"},
        output_verification_window_seconds=0,
    )
    assert request.output_verification_window_seconds == 0


def test_create_job_persists_tree_depth(isolated_jobs_db):
    _init_jobs_db()
    created = jobs.create_job(
        agent_id="agent-tree-depth",
        agent_owner_id="worker:tree-depth",
        caller_owner_id="caller:tree-depth",
        caller_wallet_id="caller-wallet-tree-depth",
        agent_wallet_id="agent-wallet-tree-depth",
        platform_wallet_id="platform-wallet-tree-depth",
        price_cents=20,
        charge_tx_id=f"charge-{uuid.uuid4().hex}",
        input_payload={"task": "depth"},
        max_attempts=2,
        tree_depth=3,
    )
    assert created["tree_depth"] == 3


def test_verified_contract_required_error_code_is_defined():
    assert error_codes.VERIFIED_CONTRACT_REQUIRED == "job.verified_contract_required"


def test_orchestration_depth_exceeded_error_code_is_defined():
    assert error_codes.ORCHESTRATION_DEPTH_EXCEEDED == "job.orchestration_depth_exceeded"
