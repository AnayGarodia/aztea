from __future__ import annotations

import time

import server.application as server
from core import recipes
from server.builtin_agents.constants import CODEREVIEW_AGENT_ID, TEST_GENERATOR_AGENT_ID

from tests.integration.helpers import _auth_headers, _fund_user_wallet, _register_user


def test_recipes_list_and_run_review_and_test(client, monkeypatch):
    caller = _register_user()
    _fund_user_wallet(caller, 1000)

    listed = client.get("/recipes", headers=_auth_headers(caller["raw_api_key"]))
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    recipe_ids = {item["pipeline_id"] for item in payload["recipes"]}
    assert {"modernize-python", "audit-deps", "review-and-test"} <= recipe_ids
    review_and_test = next(item for item in payload["recipes"] if item["pipeline_id"] == "review-and-test")
    assert review_and_test["default_input_schema"]["required"] == ["code"]

    calls: list[tuple[str, dict]] = []

    def fake_execute_builtin(agent_id: str, input_payload: dict) -> dict:
        calls.append((agent_id, dict(input_payload or {})))
        if agent_id == CODEREVIEW_AGENT_ID:
            return {"summary": "review complete"}
        if agent_id == TEST_GENERATOR_AGENT_ID:
            return {"tests": "generated tests"}
        raise AssertionError(f"Unexpected built-in recipe agent: {agent_id}")

    monkeypatch.setattr(server, "_execute_builtin_agent", fake_execute_builtin)

    run = client.post(
        "/recipes/review-and-test/run",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"input_payload": {"code": "print('hello')"}},
    )
    assert run.status_code == 200, run.text
    started = run.json()
    assert started["recipe_id"] == "review-and-test"
    assert started["pipeline_id"] == "review-and-test"

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
    assert final["output_payload"] == {"tests": "generated tests"}
    assert final["step_results"]["review"] == {"summary": "review complete"}
    assert final["step_results"]["tests"] == {"tests": "generated tests"}
    assert calls == [
        (CODEREVIEW_AGENT_ID, {"code": "print('hello')"}),
        (TEST_GENERATOR_AGENT_ID, {"code": "print('hello')"}),
    ]

    ensured_ids = {item["pipeline_id"] for item in recipes.ensure_builtin_recipes()}
    assert "review-and-test" in ensured_ids
