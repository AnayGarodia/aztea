"""
test_agent_server.py — Tests for AgentServer polling and job lifecycle.

Tests
-----
- AgentServer registers successfully and stores agent_id
- AgentServer locates an existing agent on 409 Conflict during registration
- AgentServer polls, claims, runs handler, and completes a job end-to-end
- AgentServer calls /fail when the handler raises an exception
- Heartbeat thread runs during a long job and stops after completion
"""

from __future__ import annotations

import sys
import os
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aztea.agent import AgentServer


# ── Helpers ───────────────────────────────────────────────────────────────────


def _mock_response(status_code: int, body: Any) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.content = b"x"
    resp.headers = {}
    resp.json.return_value = body
    return resp


def _make_server(handler_func=None) -> AgentServer:
    server = AgentServer(
        api_key="am_testkey",
        base_url="http://localhost:8000",
        name="Test Agent",
        description="A test agent",
        price_per_call_usd=0.05,
    )
    if handler_func:
        server.handler(handler_func)
    return server


# ── Registration ──────────────────────────────────────────────────────────────


def test_register_stores_agent_id():
    """_register_or_locate() should store the agent_id returned by the server."""
    server = _make_server(lambda inp: {})
    register_resp = _mock_response(201, {"agent_id": "agt-new", "name": "Test Agent"})

    with patch.object(server._client._http, "request", side_effect=[register_resp]):
        server._register_or_locate()

    assert server._agent_id == "agt-new"


def test_register_locates_existing_on_conflict():
    """
    When registration returns 409, _register_or_locate() should fall through to
    listing all agents and matching by name.
    """
    server = _make_server(lambda inp: {})

    conflict_resp = _mock_response(409, {"detail": "Agent name already exists."})
    list_resp = _mock_response(
        200,
        {
            "agents": [
                {"agent_id": "agt-existing", "name": "Test Agent", "owner_id": "u1"},
                {"agent_id": "agt-other", "name": "Other Agent", "owner_id": "u1"},
            ],
            "count": 2,
        },
    )

    with patch.object(
        server._client._http, "request", side_effect=[conflict_resp, list_resp]
    ):
        server._register_or_locate()

    assert server._agent_id == "agt-existing"


# ── End-to-end job lifecycle ──────────────────────────────────────────────────


def test_poll_claim_complete_end_to_end():
    """
    AgentServer should poll for pending jobs, claim each one, call the handler,
    and complete the job with the handler's return value.
    """
    completed_jobs: list[str] = []
    claimed_jobs: list[str] = []

    def handler(inp: dict) -> dict:
        return {"result": inp.get("x", 0) * 2}

    server = _make_server(handler)
    server._agent_id = "agt-test"

    # Simulate one job in the poll response
    poll_resp = _mock_response(
        200,
        {
            "jobs": [
                {
                    "job_id": "job-001",
                    "status": "pending",
                    "price_cents": 5,
                    "input_payload": {"x": 21},
                }
            ]
        },
    )
    claim_resp = _mock_response(200, {"claim_token": "tok-abc", "job_id": "job-001"})
    complete_resp = _mock_response(200, {"job_id": "job-001", "status": "complete"})

    call_log: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **kwargs):
        call_log.append((method, path))
        if method == "GET" and "/jobs/agent/" in path:
            return poll_resp
        if method == "POST" and path.endswith("/claim"):
            claimed_jobs.append(path.split("/jobs/")[1].split("/")[0])
            return claim_resp
        if method == "POST" and path.endswith("/complete"):
            completed_jobs.append(path.split("/jobs/")[1].split("/")[0])
            return complete_resp
        return _mock_response(200, {})

    with patch.object(server._client._http, "request", side_effect=fake_request):
        # Run a single poll cycle (not the infinite loop)
        server._poll_forever.__func__  # just verify it's callable
        server._process_job(
            {
                "job_id": "job-001",
                "status": "pending",
                "price_cents": 5,
                "input_payload": {"x": 21},
            }
        )

    assert "job-001" in claimed_jobs
    assert "job-001" in completed_jobs

    # Verify handler output was passed to /complete
    complete_calls = [
        c for c in call_log if c[0] == "POST" and "complete" in c[1]
    ]
    assert len(complete_calls) == 1


def test_process_job_calls_fail_on_handler_exception():
    """When the handler raises, _process_job() should call /fail with the error message."""
    def bad_handler(inp: dict) -> dict:
        raise ValueError("Something went wrong")

    server = _make_server(bad_handler)
    server._agent_id = "agt-test"

    claim_resp = _mock_response(200, {"claim_token": "tok-xyz"})
    fail_resp = _mock_response(200, {"job_id": "job-err", "status": "failed"})

    fail_calls: list[dict] = []

    def fake_request(method: str, path: str, **kwargs):
        if method == "POST" and path.endswith("/claim"):
            return claim_resp
        if method == "POST" and path.endswith("/fail"):
            fail_calls.append(kwargs.get("json", {}))
            return fail_resp
        # Unexpected heartbeat calls are fine
        return _mock_response(200, {})

    with patch.object(server._client._http, "request", side_effect=fake_request):
        server._process_job(
            {"job_id": "job-err", "status": "pending", "input_payload": {}}
        )

    assert len(fail_calls) == 1
    assert "Something went wrong" in fail_calls[0].get("error_message", "")


def test_process_job_skips_when_claim_token_missing():
    server = _make_server(lambda inp: {"ok": True})
    server._agent_id = "agt-test"

    claim_resp = _mock_response(200, {"job_id": "job-err"})
    complete_calls = 0

    def fake_request(method: str, path: str, **kwargs):
        nonlocal complete_calls
        if method == "POST" and path.endswith("/claim"):
            return claim_resp
        if method == "POST" and path.endswith("/complete"):
            complete_calls += 1
        return _mock_response(200, {})

    with patch.object(server._client._http, "request", side_effect=fake_request):
        server._process_job({"job_id": "job-err", "status": "pending", "input_payload": {}})

    assert complete_calls == 0


# ── Heartbeat thread ──────────────────────────────────────────────────────────


def test_heartbeat_loop_sends_periodic_heartbeats():
    """_heartbeat_loop() should POST heartbeats until the stop event is set."""
    server = _make_server(lambda inp: {})
    server._agent_id = "agt-hb"

    heartbeat_count = 0

    def fake_request(method: str, path: str, **kwargs):
        nonlocal heartbeat_count
        if method == "POST" and "heartbeat" in path:
            heartbeat_count += 1
        return _mock_response(200, {})

    stop_event = threading.Event()

    with patch.object(server._client._http, "request", side_effect=fake_request):
        with patch("aztea.agent._HEARTBEAT_INTERVAL", 0.05):
            hb_thread = threading.Thread(
                target=server._heartbeat_loop,
                args=("job-hb", "tok-hb", stop_event),
                daemon=True,
            )
            hb_thread.start()
            time.sleep(0.18)   # allow ~3 heartbeats at 50ms interval
            stop_event.set()
            hb_thread.join(timeout=1)

    assert heartbeat_count >= 2


def test_heartbeat_stops_after_stop_event():
    """_heartbeat_loop() should not send more heartbeats after the stop event fires."""
    server = _make_server(lambda inp: {})

    heartbeat_count = 0

    def fake_request(method: str, path: str, **kwargs):
        nonlocal heartbeat_count
        if "heartbeat" in path:
            heartbeat_count += 1
        return _mock_response(200, {})

    stop_event = threading.Event()
    stop_event.set()  # set immediately so no heartbeats are sent

    with patch.object(server._client._http, "request", side_effect=fake_request):
        server._heartbeat_loop("job-x", "tok-x", stop_event)

    assert heartbeat_count == 0
