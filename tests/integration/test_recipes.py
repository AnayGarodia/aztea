from __future__ import annotations

import time

import server.application as server
from core import recipes
from server.builtin_agents.constants import (
    DEPENDENCY_AUDITOR_AGENT_ID,
    SECRET_SCANNER_AGENT_ID,
)

from tests.integration.helpers import _auth_headers, _fund_user_wallet, _register_user


def test_recipes_list_and_run_secret_scan_and_audit(client, monkeypatch):
    """Recipes catalog covers the curated set and the multi-step recipe runs.

    Rewritten 2026-05-09: the previous review-and-lint test referenced
    Code Review + Linter agents, which were sunset because they
    duplicated capabilities a coding agent has natively (read + grep).
    The current recipe set is audit-deps, secret-scan-and-audit, and
    domain-health — testing secret-scan-and-audit because it exercises
    the multi-node pipeline path (scan → audit) the previous test
    targeted via review → lint.
    """
    caller = _register_user()
    _fund_user_wallet(caller, 1000)

    listed = client.get("/recipes", headers=_auth_headers(caller["raw_api_key"]))
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    recipe_ids = {item["pipeline_id"] for item in payload["recipes"]}
    assert {"audit-deps", "secret-scan-and-audit", "domain-health"} <= recipe_ids
    scan_audit = next(
        item
        for item in payload["recipes"]
        if item["pipeline_id"] == "secret-scan-and-audit"
    )
    assert set(scan_audit["default_input_schema"]["required"]) == {"content", "manifest"}

    calls: list[tuple[str, dict]] = []

    def fake_execute_builtin(agent_id: str, input_payload: dict) -> dict:
        calls.append((agent_id, dict(input_payload or {})))
        if agent_id == SECRET_SCANNER_AGENT_ID:
            return {"findings": [], "total_findings": 0}
        if agent_id == DEPENDENCY_AUDITOR_AGENT_ID:
            return {"vulnerabilities": [], "summary": "clean"}
        raise AssertionError(f"Unexpected built-in recipe agent: {agent_id}")

    monkeypatch.setattr(server, "_execute_builtin_agent", fake_execute_builtin)

    run = client.post(
        "/recipes/secret-scan-and-audit/run",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "input_payload": {
                "content": "print('hello')",
                "manifest": '{"dependencies": {}}',
            }
        },
    )
    assert run.status_code == 200, run.text
    started = run.json()
    assert started["recipe_id"] == "secret-scan-and-audit"
    assert started["pipeline_id"] == "secret-scan-and-audit"

    final = None
    for _ in range(50):
        polled = client.get(
            f"/pipelines/{started['pipeline_id']}/runs/{started['run_id']}",
            headers=_auth_headers(caller["raw_api_key"]),
        )
        assert polled.status_code == 200, polled.text
        final = polled.json()
        if final["status"] in {"complete", "failed"}:
            break
        time.sleep(0.05)

    assert final is not None
    assert final["status"] == "complete"
    # Final output is the last node's output (audit). Both step_results
    # are populated in the recorded run; the scan ran first, then audit.
    assert final["step_results"]["scan"] == {"findings": [], "total_findings": 0}
    assert final["step_results"]["audit"] == {
        "vulnerabilities": [],
        "summary": "clean",
    }
    assert calls == [
        (SECRET_SCANNER_AGENT_ID, {"content": "print('hello')"}),
        (DEPENDENCY_AUDITOR_AGENT_ID, {"manifest": '{"dependencies": {}}'}),
    ]

    ensured_ids = {item["pipeline_id"] for item in recipes.ensure_builtin_recipes()}
    assert "secret-scan-and-audit" in ensured_ids
