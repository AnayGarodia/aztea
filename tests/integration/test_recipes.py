from __future__ import annotations

import time

import server.application as server
from core import recipes
from server.builtin_agents.constants import DEPENDENCY_AUDITOR_AGENT_ID

from tests.integration.helpers import _auth_headers, _fund_user_wallet, _register_user


def test_recipes_list_and_run_audit_deps(client, monkeypatch):
    """Recipes catalog covers the curated set and a recipe executes end-to-end.

    2026-05-26 platform-pivot cull: the previous multi-step coverage
    (``secret-scan-and-audit``) was removed because the secret_scanner
    agent it fanned to is now sunset. The current curated catalog is
    ``audit-deps`` and ``domain-health``; both are single-step. This test
    keeps the recipes execution contract (list → start → poll → complete
    → step_results populated) on the single recipe shape we still ship.
    """
    caller = _register_user()
    _fund_user_wallet(caller, 1000)

    listed = client.get("/recipes", headers=_auth_headers(caller["raw_api_key"]))
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    recipe_ids = {item["pipeline_id"] for item in payload["recipes"]}
    assert {"audit-deps", "domain-health"} <= recipe_ids
    audit_recipe = next(
        item
        for item in payload["recipes"]
        if item["pipeline_id"] == "audit-deps"
    )
    assert set(audit_recipe["default_input_schema"]["required"]) == {"manifest"}

    calls: list[tuple[str, dict]] = []

    def fake_execute_builtin(agent_id: str, input_payload: dict) -> dict:
        calls.append((agent_id, dict(input_payload or {})))
        if agent_id == DEPENDENCY_AUDITOR_AGENT_ID:
            return {"vulnerabilities": [], "summary": "clean"}
        raise AssertionError(f"Unexpected built-in recipe agent: {agent_id}")

    monkeypatch.setattr(server, "_execute_builtin_agent", fake_execute_builtin)

    run = client.post(
        "/recipes/audit-deps/run",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"input_payload": {"manifest": '{"dependencies": {}}'}},
    )
    assert run.status_code == 200, run.text
    started = run.json()
    assert started["recipe_id"] == "audit-deps"
    assert started["pipeline_id"] == "audit-deps"

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
    assert final["step_results"]["audit"] == {
        "vulnerabilities": [],
        "summary": "clean",
    }
    assert calls == [
        (DEPENDENCY_AUDITOR_AGENT_ID, {"manifest": '{"dependencies": {}}'}),
    ]

    ensured_ids = {item["pipeline_id"] for item in recipes.ensure_builtin_recipes()}
    assert "audit-deps" in ensured_ids
    # Stale recipes from prior deploys (secret-scan-and-audit and
    # security-audit-sealed) must be pruned by ensure_builtin_recipes.
    assert "secret-scan-and-audit" not in ensured_ids
    assert "security-audit-sealed" not in ensured_ids
