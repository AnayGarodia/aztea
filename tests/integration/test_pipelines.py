from __future__ import annotations

import time
import uuid

from core import payments
from core.pipelines import executor as pipeline_executor

from tests.integration.helpers import (
    _auth_headers,
    _fund_user_wallet,
    _register_agent_via_api,
    _register_user,
)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200
        self.ok = True

    def json(self):
        return self._payload


def test_pipeline_run_executes_nodes_in_order_and_returns_terminal_output(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    wallet = _fund_user_wallet(caller, 500)
    agent_a = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Pipeline Agent A {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["pipeline"],
    )
    agent_b = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Pipeline Agent B {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["pipeline"],
    )

    calls: list[tuple[str, dict]] = []

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        del headers, timeout, allow_redirects
        calls.append((url, dict(json or {})))
        task = (json or {}).get("task")
        if task == "source text":
            return _FakeResponse({"content": "normalized text"})
        if (json or {}).get("code") == "normalized text":
            return _FakeResponse({"summary": "review complete"})
        raise AssertionError(f"Unexpected pipeline call payload: {json!r}")

    monkeypatch.setattr(pipeline_executor.requests, "post", fake_post)

    pipeline = client.post(
        "/pipelines",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "name": f"Pipeline {uuid.uuid4().hex[:6]}",
            "definition": {
                "nodes": [
                    {
                        "id": "research",
                        "agent_id": agent_a,
                        "input_map": {"task": "$input.task"},
                    },
                    {
                        "id": "review",
                        "agent_id": agent_b,
                        "depends_on": ["research"],
                        "input_map": {"code": "$research.output.content"},
                    },
                ]
            },
        },
    )
    assert pipeline.status_code == 201, pipeline.text
    pipeline_id = pipeline.json()["pipeline_id"]

    run = client.post(
        f"/pipelines/{pipeline_id}/run",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"input_payload": {"task": "source text"}},
    )
    assert run.status_code == 200, run.text
    run_id = run.json()["run_id"]

    final = None
    for _ in range(50):
        polled = client.get(
            f"/pipelines/{pipeline_id}/runs/{run_id}",
            headers=_auth_headers(caller["raw_api_key"]),
        )
        assert polled.status_code == 200, polled.text
        final = polled.json()
        if final["status"] in {"complete", "failed"}:
            break
        time.sleep(0.05)

    assert final is not None
    assert final["status"] == "complete"
    assert final["output_payload"] == {"summary": "review complete"}
    assert final["step_results"]["research"] == {"content": "normalized text"}
    assert final["step_results"]["review"] == {"summary": "review complete"}
    assert payments.get_wallet(wallet["wallet_id"])["balance_cents"] == 478
    assert len(calls) == 2
    assert calls[0][1] == {"task": "source text"}
    assert calls[1][1] == {"code": "normalized text"}
