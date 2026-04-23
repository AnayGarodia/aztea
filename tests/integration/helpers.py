"""Shared integration-test helpers (imported by test modules; not collected as tests)."""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import requests
import uvicorn
from fastapi.testclient import TestClient

from core import auth
from core import disputes
from core import jobs
from core import payments
from core import registry
from core import reputation
from core import error_codes
import server.application as server

TEST_MASTER_KEY = "test-master-key"


def _close_module_conn(module) -> None:
    conn = getattr(module._local, "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(module._local, "conn")
    except AttributeError:
        pass


def _auth_headers(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _register_user() -> dict:
    suffix = uuid.uuid4().hex[:8]
    return auth.register_user(
        username=f"user-{suffix}",
        email=f"user-{suffix}@example.com",
        password="password123",
    )


def _register_agent_via_api(
    client: TestClient,
    raw_api_key: str,
    *,
    name: str,
    price: float = 0.10,
    tags: list[str] | None = None,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    output_verifier_url: str | None = None,
    auto_approve: bool = True,
) -> str:
    default_input_schema = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "title": "Task",
                "description": "Integration-test task input.",
            }
        },
    }
    payload = {
        "name": name,
        "description": "integration test agent",
        "endpoint_url": f"https://agents.example.com/{uuid.uuid4().hex[:8]}",
        "price_per_call_usd": price,
        "tags": tags or ["integration-test"],
        "input_schema": input_schema or default_input_schema,
        "output_examples": [
            {"input": {"task": "analyze"}, "output": {"ok": True}},
        ],
    }
    if output_schema is not None:
        payload["output_schema"] = output_schema
    if output_verifier_url is not None:
        payload["output_verifier_url"] = output_verifier_url
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(raw_api_key),
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["agent_id"]
    if auto_approve:
        review = client.post(
            f"/admin/agents/{agent_id}/review",
            headers=_auth_headers(TEST_MASTER_KEY),
            json={"decision": "approve", "note": "test auto-approve"},
        )
        assert review.status_code == 200, review.text
    return agent_id


def _fund_user_wallet(user: dict, amount_cents: int = 500) -> dict:
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")
    payments.deposit(wallet["wallet_id"], amount_cents, "integration test funds")
    return wallet


def _create_job_via_api(
    client: TestClient,
    raw_api_key: str,
    *,
    agent_id: str,
    max_attempts: int = 3,
    extra: dict | None = None,
) -> dict:
    payload = {
        "agent_id": agent_id,
        "input_payload": {"task": "analyze"},
        "max_attempts": max_attempts,
    }
    if extra:
        payload.update(extra)
    resp = client.post(
        "/jobs",
        headers=_auth_headers(raw_api_key),
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _force_settle_completed_job(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    assert job is not None
    assert job["status"] == "complete"
    window_seconds = max(1, int(server._effective_dispute_window_seconds(job)))
    completed_at = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds + 5)).isoformat()
    expired_verification_deadline = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET completed_at = ?,
                updated_at = ?,
                output_verification_deadline_at = CASE
                    WHEN output_verification_status = 'pending' THEN ?
                    ELSE output_verification_deadline_at
                END
            WHERE job_id = ?
            """,
            (completed_at, completed_at, expired_verification_deadline, job_id),
        )
    server._sweep_jobs(
        retry_delay_seconds=0,
        sla_seconds=7200,
        limit=100,
        actor_owner_id="test:settlement",
    )
    settled = jobs.get_job(job_id)
    assert settled is not None
    return settled


def _manifest(
    name: str,
    endpoint_url: str,
    *,
    output_schema: dict | None = None,
    output_verifier_url: str | None = None,
) -> str:
    metadata = {
        "name": name,
        "description": "Manifest onboarded agent",
        "endpoint_url": endpoint_url,
        "price_per_call_usd": 0.05,
        "tags": ["manifest-test"],
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "title": "Task",
                    "description": "manifest test input",
                }
            },
        },
        "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
    }
    if output_schema is not None:
        metadata["output_schema"] = output_schema
    if output_verifier_url is not None:
        metadata["output_verifier_url"] = output_verifier_url
    metadata_json = json.dumps(metadata, indent=2)

    return f"""# Example Agent Manifest

## Registry Endpoint
Use POST /registry/register.

## Registration Flow
Validate then register.

## Job Acceptance/Claim Flow Expectations
Workers claim with lease + claim_token.

## Settlement Flow Expectations
Success pays out, failure refunds.

## Auth Expectations
Bearer API key auth is required.

## Registration Metadata
```json
{metadata_json}
```
"""
