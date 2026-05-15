"""End-to-end pins for structured error envelopes.

When something blows up inside ``jobs.create_job``, the caller must see a
machine-readable envelope — ``{error: "job.create_failed", message, details}``
with ``refunded_cents`` — not a bare ``"Failed to create job."`` string. PR 1
of the silent-failures sweep replaced three vague 500s; this file pins the
shape so they don't regress.
"""

from __future__ import annotations

import pytest

from tests.integration.support import *  # noqa: F403


def test_job_create_failure_returns_structured_envelope(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Envelope Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["error-envelope-test"],
    )

    # Force the async create path (server/application_parts/part_008.py:2785)
    # to take its except branch: jobs.create_job blows up.
    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(jobs, "create_job", _boom)

    resp = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": agent_id, "input_payload": {"task": "x"}},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    # FastAPI wraps the structured make_error() dict under `detail`. Tolerate
    # both flat and nested shapes so the assertion survives middleware shifts.
    err_envelope = body.get("detail") if isinstance(body.get("detail"), dict) else body
    assert err_envelope.get("error") == "job.create_failed", body
    assert "refunded" in err_envelope.get("message", "").lower(), body
    details = err_envelope.get("details") or {}
    assert details.get("agent_id") == agent_id, body
    assert isinstance(details.get("refunded_cents"), int), body
    assert details.get("refunded_cents") >= 10, body
    assert details.get("underlying") == "RuntimeError", body
