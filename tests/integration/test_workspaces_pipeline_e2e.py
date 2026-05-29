"""End-to-end: recipe with ``auto_workspace=True`` creates, populates, seals.

Pipelines are async (run in a daemon thread), so these tests poll for the
terminal status before asserting on workspace state.
"""

from __future__ import annotations

import json
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
        self._body = json.dumps(payload).encode()
        self.status_code = 200
        self.ok = True
        self.headers = {}

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=None):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _poll_until_terminal(client, caller_key, pipeline_id, run_id, timeout_s=5.0):
    deadline = time.time() + timeout_s
    final = None
    while time.time() < deadline:
        r = client.get(
            f"/pipelines/{pipeline_id}/runs/{run_id}",
            headers=_auth_headers(caller_key),
        )
        assert r.status_code == 200, r.text
        final = r.json()
        if final["status"] in {"complete", "failed"}:
            return final
        time.sleep(0.05)
    raise AssertionError(f"pipeline did not finish in {timeout_s}s; last={final}")


def test_auto_workspace_recipe_creates_seals_records_outputs(client, monkeypatch):
    """Two-step recipe with auto_workspace=true.

    Verifies that each step's output lands in the workspace under
    outputs/{agent_slug}/{node_id}.json, the run row carries
    workspace_id, the workspace is sealed at the end, and the manifest
    verifies cleanly.
    """
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    agent_a = _register_agent_via_api(
        client, worker["raw_api_key"],
        name=f"WS Recipe A {uuid.uuid4().hex[:6]}",
        price=0.10, tags=["ws-recipe"],
    )
    agent_b = _register_agent_via_api(
        client, worker["raw_api_key"],
        name=f"WS Recipe B {uuid.uuid4().hex[:6]}",
        price=0.10, tags=["ws-recipe"],
    )

    def fake_post(url, data=None, json=None, headers=None, timeout=None,
                  allow_redirects=None, stream=False):
        del url, headers, timeout, allow_redirects, stream
        # Plan B Phase 1: HMAC-signed dispatch sends bytes via `data=`; the
        # legacy unsigned path still uses `json=`. Accept either.
        if data is not None:
            import json as _json
            payload = _json.loads(data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data)
        else:
            payload = dict(json or {})
        task = payload.get("task")
        if task is not None:
            return _FakeResponse({"step1_out": f"processed:{task}"})
        if payload.get("upstream") is not None:
            return _FakeResponse({"final": "summary-built"})
        raise AssertionError(f"unexpected pipeline payload: {payload!r}")

    monkeypatch.setattr(pipeline_executor.requests, "post", fake_post)

    # Create pipeline with auto_workspace=true.
    pipeline = client.post(
        "/pipelines",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "name": f"WS Pipeline {uuid.uuid4().hex[:6]}",
            "definition": {
                "auto_workspace": True,
                "nodes": [
                    {
                        "id": "step1",
                        "agent_id": agent_a,
                        "input_map": {"task": "$input.task"},
                    },
                    {
                        "id": "step2",
                        "agent_id": agent_b,
                        "depends_on": ["step1"],
                        "input_map": {"upstream": "$step1.output.step1_out"},
                    },
                ],
            },
        },
    )
    assert pipeline.status_code == 201, pipeline.text
    pipeline_id = pipeline.json()["pipeline_id"]

    run = client.post(
        f"/pipelines/{pipeline_id}/run",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"input_payload": {"task": "hello"}},
    )
    assert run.status_code == 200, run.text
    run_id = run.json()["run_id"]

    final = _poll_until_terminal(client, caller["raw_api_key"], pipeline_id, run_id)
    assert final["status"] == "complete"

    # Run row carries the workspace_id.
    workspace_id = final.get("workspace_id")
    assert workspace_id and workspace_id.startswith("ws_"), final

    # Workspace is sealed.
    ws = client.get(
        f"/workspaces/{workspace_id}",
        headers=_auth_headers(caller["raw_api_key"]),
    ).json()
    assert ws["status"] == "sealed", ws

    # Both step outputs landed as artifacts.
    listing = client.get(
        f"/workspaces/{workspace_id}/artifacts",
        headers=_auth_headers(caller["raw_api_key"]),
    ).json()["artifacts"]
    names = {a["name"] for a in listing}
    assert any(n.startswith("outputs/") and "step1" in n for n in names), names
    assert any(n.startswith("outputs/") and "step2" in n for n in names), names

    # Public verify passes.
    verify = client.post(f"/workspaces/{workspace_id}/verify").json()
    assert verify["valid"] is True
    assert verify["signer_did"].endswith(":workspaces:sealer")


def test_recipe_without_auto_workspace_creates_no_workspace(client, monkeypatch):
    """Default behaviour unchanged: recipes that don't opt in get no workspace."""
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 500)
    agent = _register_agent_via_api(
        client, worker["raw_api_key"],
        name=f"Plain {uuid.uuid4().hex[:6]}",
        price=0.10, tags=["plain"],
    )

    monkeypatch.setattr(
        pipeline_executor.requests, "post",
        lambda *a, **kw: _FakeResponse({"ok": True}),
    )

    pipeline = client.post(
        "/pipelines",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "name": f"Plain {uuid.uuid4().hex[:6]}",
            "definition": {
                "nodes": [
                    {"id": "only", "agent_id": agent,
                     "input_map": {"task": "$input.task"}},
                ],
            },
        },
    ).json()

    run = client.post(
        f"/pipelines/{pipeline['pipeline_id']}/run",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"input_payload": {"task": "x"}},
    ).json()

    final = _poll_until_terminal(
        client, caller["raw_api_key"], pipeline["pipeline_id"], run["run_id"],
    )
    assert final["status"] == "complete"
    assert final.get("workspace_id") in (None, "")
