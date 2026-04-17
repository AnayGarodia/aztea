"""
test_client.py — Tests for AgentMarketClient.hire() using a mock HTTP transport.

Tests
-----
- hire() happy path returns JobResult with correct output and cost_cents
- hire() raises JobFailedError when job status is "failed"
- hire() raises ContractVerificationError when output violates contract
- hire() raises AgentNotFoundError on 404 from POST /jobs
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

# Put the sdk package on the path when running from the sdk/ directory
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentmarket.client import AgentMarketClient
from agentmarket.exceptions import (
    AgentMarketError,
    AgentNotFoundError,
    ContractVerificationError,
    JobFailedError,
    RateLimitError,
)
from agentmarket.models import JobResult


# ── Helpers ───────────────────────────────────────────────────────────────────


def _mock_response(status_code: int, body: Any) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.content = b"x"  # non-empty so json() is called
    resp.headers = {}
    resp.json.return_value = body
    return resp


def _make_client() -> AgentMarketClient:
    client = AgentMarketClient(api_key="am_testkey", base_url="http://localhost:8000")
    return client


# ── hire() happy path ─────────────────────────────────────────────────────────


def test_hire_happy_path():
    """hire() should poll until complete and return a populated JobResult."""
    client = _make_client()

    create_resp = _mock_response(200, {"job_id": "job-abc", "price_cents": 10, "status": "pending"})
    poll_pending = _mock_response(200, {"job_id": "job-abc", "status": "pending", "price_cents": 10})
    poll_complete = _mock_response(
        200,
        {
            "job_id": "job-abc",
            "status": "complete",
            "price_cents": 10,
            "output_payload": {"company_name": "Anthropic", "founded_year": 2021},
            "quality_score": 5,
        },
    )

    call_sequence = [create_resp, poll_pending, poll_complete]

    with patch.object(client._http, "request", side_effect=call_sequence):
        with patch("agentmarket.client.time.sleep"):  # skip real sleep
            result = client.hire("agent-id", {"url": "https://anthropic.com"})

    assert isinstance(result, JobResult)
    assert result.job_id == "job-abc"
    assert result.output == {"company_name": "Anthropic", "founded_year": 2021}
    assert result.cost_cents == 10
    assert result.quality_score == 5


# ── hire() raises JobFailedError ──────────────────────────────────────────────


def test_hire_raises_job_failed_error():
    """hire() should raise JobFailedError when the job reaches 'failed' status."""
    client = _make_client()

    create_resp = _mock_response(200, {"job_id": "job-fail", "price_cents": 10, "status": "pending"})
    poll_failed = _mock_response(
        200,
        {
            "job_id": "job-fail",
            "status": "failed",
            "price_cents": 10,
            "error_message": "Worker timed out.",
            "output_payload": None,
        },
    )

    with patch.object(client._http, "request", side_effect=[create_resp, poll_failed]):
        with patch("agentmarket.client.time.sleep"):
            with pytest.raises(JobFailedError) as exc_info:
                client.hire("agent-id", {"text": "hello"})

    err = exc_info.value
    assert "Worker timed out" in str(err)
    assert err.output == {}


# ── hire() raises ContractVerificationError ───────────────────────────────────


def test_hire_raises_contract_verification_error_missing_key():
    """hire() should raise ContractVerificationError when required_keys are absent."""
    client = _make_client()

    create_resp = _mock_response(200, {"job_id": "job-cv", "price_cents": 5})
    poll_complete = _mock_response(
        200,
        {
            "job_id": "job-cv",
            "status": "complete",
            "price_cents": 5,
            # output is missing "company_name" which the contract requires
            "output_payload": {"founded_year": 2021},
        },
    )

    contract = {"required_keys": ["company_name"], "field_types": {}, "field_ranges": {}}

    with patch.object(client._http, "request", side_effect=[create_resp, poll_complete]):
        with patch("agentmarket.client.time.sleep"):
            with pytest.raises(ContractVerificationError) as exc_info:
                client.hire("agent-id", {"url": "x"}, verification_contract=contract)

    err = exc_info.value
    assert any("company_name" in f for f in err.failures)


def test_hire_raises_contract_verification_error_wrong_type():
    """hire() should raise ContractVerificationError when a field has the wrong type."""
    client = _make_client()

    create_resp = _mock_response(200, {"job_id": "job-type", "price_cents": 5})
    poll_complete = _mock_response(
        200,
        {
            "job_id": "job-type",
            "status": "complete",
            "price_cents": 5,
            # founded_year should be a number but came back as a string
            "output_payload": {"company_name": "Acme", "founded_year": "2021"},
        },
    )

    contract = {
        "required_keys": [],
        "field_types": {"founded_year": "number"},
        "field_ranges": {},
    }

    with patch.object(client._http, "request", side_effect=[create_resp, poll_complete]):
        with patch("agentmarket.client.time.sleep"):
            with pytest.raises(ContractVerificationError) as exc_info:
                client.hire("agent-id", {"url": "x"}, verification_contract=contract)

    err = exc_info.value
    assert any("founded_year" in f for f in err.failures)
    assert len(err.failures) == 1


def test_hire_raises_contract_verification_error_range():
    """hire() should raise ContractVerificationError when a numeric field is out of range."""
    client = _make_client()

    create_resp = _mock_response(200, {"job_id": "job-range", "price_cents": 5})
    poll_complete = _mock_response(
        200,
        {
            "job_id": "job-range",
            "status": "complete",
            "price_cents": 5,
            "output_payload": {"score": 150},  # max is 100
        },
    )

    contract = {
        "required_keys": [],
        "field_types": {},
        "field_ranges": {"score": {"min": 0, "max": 100}},
    }

    with patch.object(client._http, "request", side_effect=[create_resp, poll_complete]):
        with patch("agentmarket.client.time.sleep"):
            with pytest.raises(ContractVerificationError) as exc_info:
                client.hire("agent-id", {"x": 1}, verification_contract=contract)

    assert any("above maximum" in f for f in exc_info.value.failures)


# ── hire() 404 on create ──────────────────────────────────────────────────────


def test_hire_raises_agent_not_found_on_create():
    """hire() should raise AgentNotFoundError when the server returns 404 for POST /jobs."""
    client = _make_client()

    not_found = _mock_response(404, {"detail": "Agent 'bad-id' not found."})

    with patch.object(client._http, "request", side_effect=[not_found]):
        with pytest.raises(AgentNotFoundError) as exc_info:
            client.hire("bad-id", {"text": "x"})

    assert exc_info.value.status_code == 404


# ── hire() wait=False ─────────────────────────────────────────────────────────


def test_hire_no_wait_returns_immediately():
    """hire(wait=False) should return after creating the job without polling."""
    client = _make_client()

    create_resp = _mock_response(200, {"job_id": "job-nw", "price_cents": 10})

    with patch.object(client._http, "request", side_effect=[create_resp]):
        result = client.hire("agent-id", {}, wait=False)

    assert result.job_id == "job-nw"
    assert result.output == {}


def test_request_429_with_invalid_retry_after_uses_default():
    client = _make_client()
    too_many = _mock_response(429, {"detail": "Too many requests"})
    too_many.headers = {"Retry-After": "abc"}

    with patch.object(client._http, "request", side_effect=[too_many]):
        with pytest.raises(RateLimitError) as exc_info:
            client.get_balance()

    assert exc_info.value.retry_after == 60


def test_hire_raises_clear_error_when_job_id_missing():
    client = _make_client()
    create_resp = _mock_response(200, {"price_cents": 10})

    with patch.object(client._http, "request", side_effect=[create_resp]):
        with pytest.raises(AgentMarketError) as exc_info:
            client.hire("agent-id", {}, wait=False)

    assert "missing a valid job_id" in str(exc_info.value)


def test_hire_includes_callback_secret_in_job_create_payload():
    client = _make_client()
    create_resp = _mock_response(200, {"job_id": "job-cb", "price_cents": 10})

    with patch.object(client._http, "request", side_effect=[create_resp]) as req_mock:
        result = client.hire(
            "agent-id",
            {"task": "x"},
            wait=False,
            callback_url="https://hooks.example.com/job",
            callback_secret="cb-secret",
        )

    assert result.job_id == "job-cb"
    first_call_kwargs = req_mock.call_args_list[0].kwargs
    assert first_call_kwargs["json"]["callback_url"] == "https://hooks.example.com/job"
    assert first_call_kwargs["json"]["callback_secret"] == "cb-secret"


def test_hire_includes_lineage_and_timeout_controls():
    client = _make_client()
    create_resp = _mock_response(200, {"job_id": "job-lineage", "price_cents": 10})

    with patch.object(client._http, "request", side_effect=[create_resp]) as req_mock:
        result = client.hire(
            "agent-id",
            {"task": "delegate"},
            wait=False,
            parent_job_id="job-parent",
            parent_cascade_policy="fail_children_on_parent_fail",
            clarification_timeout_seconds=120,
            clarification_timeout_policy="proceed",
            output_verification_window_seconds=300,
        )

    assert result.job_id == "job-lineage"
    first_call_kwargs = req_mock.call_args_list[0].kwargs
    payload = first_call_kwargs["json"]
    assert payload["parent_job_id"] == "job-parent"
    assert payload["parent_cascade_policy"] == "fail_children_on_parent_fail"
    assert payload["clarification_timeout_seconds"] == 120
    assert payload["clarification_timeout_policy"] == "proceed"
    assert payload["output_verification_window_seconds"] == 300


def test_decide_output_verification_posts_expected_payload():
    client = _make_client()
    decision_resp = _mock_response(
        200,
        {
            "job_id": "job-verify",
            "agent_id": "agent-id",
            "status": "complete",
            "price_cents": 10,
            "input_payload": {},
            "output_payload": {"ok": True},
            "output_verification_status": "accepted",
        },
    )

    with patch.object(client._http, "request", side_effect=[decision_resp]) as req_mock:
        job = client.decide_output_verification(
            "job-verify",
            decision="accept",
            reason="Looks good",
        )

    assert job.job_id == "job-verify"
    kwargs = req_mock.call_args_list[0].kwargs
    assert kwargs["json"]["decision"] == "accept"
    assert kwargs["json"]["reason"] == "Looks good"


def test_search_agents_returns_reputation_signals():
    client = _make_client()
    search_resp = _mock_response(
        200,
        {
            "results": [
                {
                    "agent": {
                        "agent_id": "agt-demo",
                        "name": "Demo Agent",
                        "description": "Does demo work",
                        "endpoint_url": "https://agents.example.com/demo",
                        "price_per_call_usd": 0.12,
                        "trust_score": 0.92,
                        "success_rate": 0.97,
                        "total_calls": 110,
                        "successful_calls": 107,
                        "avg_latency_ms": 420.5,
                        "dispute_rate": 0.01,
                        "input_schema": {},
                        "output_schema": {},
                        "tags": ["demo"],
                        "owner_id": "owner-demo",
                    }
                }
            ]
        },
    )

    with patch.object(client._http, "request", side_effect=[search_resp]):
        agents = client.search_agents("demo")

    assert len(agents) == 1
    assert agents[0].trust_score == pytest.approx(0.92)
    assert agents[0].success_rate == pytest.approx(0.97)
    assert agents[0].total_calls == 110
    assert agents[0].avg_latency_ms == pytest.approx(420.5)
    assert agents[0].dispute_rate == pytest.approx(0.01)
