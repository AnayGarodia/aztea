from __future__ import annotations

from typing import Any

from scripts import aztea_mcp_meta_tools as meta_tools

from tests.integration.helpers import (
    _auth_headers,
    _create_job_via_api,
    _fund_user_wallet,
    _register_agent_via_api,
    _register_user,
)


class _ResponseAdapter:
    def __init__(self, response) -> None:
        self._response = response
        self.ok = response.status_code < 400
        self.status_code = response.status_code
        self.text = response.text

    def json(self):
        return self._response.json()


class _ClientSession:
    def __init__(self, client) -> None:
        self._client = client

    def get(self, url: str, headers: dict[str, str] | None = None, timeout: float | None = None, **kwargs: Any):
        del timeout
        return _ResponseAdapter(self._client.get(url, headers=headers, **kwargs))

    def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        json: Any | None = None,
        **kwargs: Any,
    ):
        del timeout
        return _ResponseAdapter(self._client.post(url, headers=headers, json=json, **kwargs))


def _call_meta_tool(client, api_key: str, tool_name: str, arguments: dict[str, Any], session_state: dict[str, Any] | None = None):
    ok, result = meta_tools.call_meta_tool(
        session=_ClientSession(client),
        base_url="",
        api_key=api_key,
        tool_name=tool_name,
        arguments=arguments,
        session_state=session_state or {"budget_cents": None, "spent_cents": 0},
        timeout=5,
    )
    return ok, result


def test_mcp_async_job_lifecycle_supports_clarify_verify_rate_and_dispute(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    agent_id = _register_agent_via_api(client, worker["raw_api_key"], name="Async MCP Lifecycle Agent")

    session_state = {"budget_cents": None, "spent_cents": 0}
    ok_hire, hired = _call_meta_tool(
        client,
        caller["raw_api_key"],
        "aztea_hire_async",
        {"agent_id": agent_id, "input_payload": {"task": "analyze repo and ask a question"}},
        session_state=session_state,
    )
    assert ok_hire is True
    job_id = hired["job_id"]
    assert session_state["spent_cents"] == int(hired["caller_charge_cents"])

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    ask = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "clarification_request", "payload": {"question": "Which files should I inspect?"}},
    )
    assert ask.status_code == 201, ask.text
    request_message_id = ask.json()["message_id"]

    ok_status, status = _call_meta_tool(
        client,
        caller["raw_api_key"],
        "aztea_job_status",
        {"job_id": job_id},
        session_state=session_state,
    )
    assert ok_status is True
    assert status["status"] == "awaiting_clarification"
    assert status["clarification_needed"]["question"] == "Which files should I inspect?"
    assert any(msg["type"] == "clarification_request" for msg in status["messages"])

    ok_clarify, clarified = _call_meta_tool(
        client,
        caller["raw_api_key"],
        "aztea_clarify",
        {"job_id": job_id, "message": "Start with the payment and MCP files."},
        session_state=session_state,
    )
    assert ok_clarify is True
    assert "note" in clarified

    messages = client.get(f"/jobs/{job_id}/messages", headers=_auth_headers(caller["raw_api_key"]))
    assert messages.status_code == 200, messages.text
    assert any(
        msg["type"] == "clarification_response"
        and msg["payload"]["answer"] == "Start with the payment and MCP files."
        and str(msg["payload"]["request_message_id"]) == str(request_message_id)
        for msg in messages.json()["messages"]
    )

    complete = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "claim_token": claim_token,
            "output_payload": {"summary": "Done.", "billing_units_actual": 2},
        },
    )
    assert complete.status_code == 200, complete.text

    ok_status_complete, complete_status = _call_meta_tool(
        client,
        caller["raw_api_key"],
        "aztea_job_status",
        {"job_id": job_id},
        session_state=session_state,
    )
    assert ok_status_complete is True
    assert complete_status["status"] == "complete"
    assert complete_status["output_payload"]["summary"] == "Done."
    assert complete_status["output_verification_status"] == "pending"

    ok_verify, verified = _call_meta_tool(
        client,
        caller["raw_api_key"],
        "aztea_verify_output",
        {"job_id": job_id, "decision": "accept"},
        session_state=session_state,
    )
    assert ok_verify is True
    assert verified["output_verification_status"] == "accepted"

    ok_rate, rated = _call_meta_tool(
        client,
        caller["raw_api_key"],
        "aztea_rate_job",
        {"job_id": job_id, "rating": 5},
        session_state=session_state,
    )
    assert ok_rate is True
    assert rated["rating"]["rating"] == 5

    second_job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, extra={"output_verification_window_seconds": 0})
    second_claim = client.post(
        f"/jobs/{second_job['job_id']}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert second_claim.status_code == 200, second_claim.text

    second_complete = client.post(
        f"/jobs/{second_job['job_id']}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "claim_token": second_claim.json()["claim_token"],
            "output_payload": {"summary": "Bad output."},
        },
    )
    assert second_complete.status_code == 200, second_complete.text

    ok_dispute, disputed = _call_meta_tool(
        client,
        caller["raw_api_key"],
        "aztea_dispute_job",
        {"job_id": second_job["job_id"], "reason": "The output is incorrect.", "evidence": "Mismatch against expected result."},
        session_state=session_state,
    )
    assert ok_dispute is True
    assert disputed["job_id"] == second_job["job_id"]
    assert disputed["status"] in {"pending", "judging", "consensus", "tied", "resolved", "appealed", "final"}
