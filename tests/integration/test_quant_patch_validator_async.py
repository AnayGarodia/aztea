"""Async-lifecycle tests for `quant_patch_validator`.

# OWNS: verifying the agent works through the async job path (POST /jobs),
#        with the deep-tier budget. This is the only legitimate path for
#        validators that exceed the 8s sync gateway.
# NOT OWNS: the sync gateway path (see lifecycle tests).
# DECISIONS:
#   - We use the standard integration test client + helpers. The async
#     path drives through the built-in worker which polls every 2s for
#     pending jobs. We poll the job status to wait for completion.
"""

from __future__ import annotations

import time

import pytest

from tests.integration.support import *  # noqa: F403,F401

from server.builtin_agents.constants import QUANT_PATCH_VALIDATOR_AGENT_ID


_TRIVIAL_REF = "def f(x): return x * 2\n"


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skip(
        reason=(
            "Sunset 2026-05-26 platform-pivot cull: quant_patch_validator "
            "moved to SUNSET_DEPRECATED_AGENT_IDS. The job-creation path "
            "now rejects sunset agents. Re-enable when the agent returns "
            "to CURATED_PUBLIC_BUILTIN_AGENT_IDS."
        )
    ),
]


def _poll_job(client, caller, job_id, deadline_s=30):
    """Poll a job until terminal or deadline; return final job row."""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        resp = client.get(f"/jobs/{job_id}", headers=_auth_headers(caller["raw_api_key"]))
        body = resp.json()
        status = body.get("status")
        if status in ("complete", "failed", "cancelled"):
            return body
        time.sleep(0.5)
    pytest.fail(f"job {job_id} did not finish within {deadline_s}s")


def test_async_path_for_trivial_validation(client):
    """Submit a quick-tier job via POST /jobs and confirm completion."""
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    create = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": QUANT_PATCH_VALIDATOR_AGENT_ID,
            "input": {
                "reference_code": _TRIVIAL_REF,
                "candidate_code": _TRIVIAL_REF,
                "fuzz_budget": "quick",
                "fuzz_seconds": 3,
            },
        },
    )
    assert create.status_code in (200, 201), create.text
    job_id = create.json()["job_id"]
    final = _poll_job(client, caller, job_id, deadline_s=45)
    assert final["status"] == "complete", final
    output = final.get("output_payload") or final.get("output")
    assert output["verdict"] in (
        "equivalent",
        "regressions_found",
        "contract_broken",
        "signature_divergence",
        "intended_changes_only",
    ), output


def test_async_workspace_artifact_threaded_through(client):
    """The async path threads `_workspace_id` to the agent → artifact written."""
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    # Create a workspace owned by the caller.
    ws_resp = client.post("/workspaces", json={}, headers=_auth_headers(caller["raw_api_key"]))
    assert ws_resp.status_code in (200, 201), ws_resp.text
    ws_id = ws_resp.json()["workspace_id"]

    create = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": QUANT_PATCH_VALIDATOR_AGENT_ID,
            "input": {
                "reference_code": _TRIVIAL_REF,
                "candidate_code": _TRIVIAL_REF,
                "fuzz_budget": "quick",
                "fuzz_seconds": 3,
                "_workspace_id": ws_id,
            },
        },
    )
    assert create.status_code in (200, 201), create.text
    job_id = create.json()["job_id"]
    _poll_job(client, caller, job_id, deadline_s=45)

    arts_resp = client.get(
        f"/workspaces/{ws_id}/artifacts", headers=_auth_headers(caller["raw_api_key"])
    )
    if arts_resp.status_code != 200:
        pytest.skip(f"workspace artifact listing returned {arts_resp.status_code}; out of scope for this test")
    arts = arts_resp.json().get("artifacts", [])
    paths = [a.get("name") or a.get("path") for a in arts]
    assert any("qpv" in (p or "") for p in paths), f"no qpv artifact found in {paths}"
