from __future__ import annotations

from scripts import aztea_mcp_meta_tools as meta_tools


class _FakeResponse:
    def __init__(self, *, ok: bool, status_code: int, payload):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_parse_surfaces_nested_refund_metadata():
    ok, result = meta_tools._parse(
        _FakeResponse(
            ok=False,
            status_code=402,
            payload={
                "detail": {
                    "message": "Agent execution failed.",
                    "data": {
                        "refunded": True,
                        "refund_amount_cents": 5,
                        "cost_usd": 0,
                        "wallet_balance_cents": 95,
                    },
                }
            },
        )
    )
    assert ok is False
    assert result["error"] == "API_ERROR"
    assert result["message"] == "Agent execution failed."
    assert result["refunded"] is True
    assert result["refund_amount_cents"] == 5
    assert result["cost_usd"] == 0
    assert result["wallet_balance_cents"] == 95


def test_meta_tool_catalog_exposes_claude_control_plane():
    tool_names = {tool["name"] for tool in meta_tools.get_meta_tools()}
    assert {
        "aztea_estimate_cost",
        "aztea_hire_async",
        "aztea_job_status",
        "aztea_compare_agents",
        "aztea_compare_status",
        "aztea_list_recipes",
        "aztea_run_recipe",
        "aztea_list_pipelines",
        "aztea_run_pipeline",
        "aztea_pipeline_status",
    } <= tool_names


def test_verify_output_uses_jobs_verification_route(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["url"] = url
        captured["body"] = body
        return True, {"status": "ok"}

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._verify_output(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"job_id": "job_123", "decision": "accept"},
    )
    assert ok is True
    assert result["status"] == "ok"
    assert captured["url"] == "https://aztea.test/jobs/job_123/verification"
    assert captured["body"] == {"decision": "accept"}


def test_clarify_posts_answer_payload(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["url"] = url
        captured["body"] = body
        return True, {"status": "ok"}

    def _fake_get(_session, _url, _hdrs, _timeout, **_kwargs):
        return True, {"messages": [{"message_id": 7, "type": "clarification_request", "payload": {"question": "?"}}]}

    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._clarify(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"job_id": "job_123", "message": "Check the MCP files."},
    )
    assert ok is True
    assert result["status"] == "ok"
    assert captured["url"] == "https://aztea.test/jobs/job_123/messages"
    assert captured["body"] == {
        "type": "clarification_response",
        "payload": {"answer": "Check the MCP files.", "request_message_id": 7},
    }


def test_estimate_cost_posts_to_agent_estimate_route(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["url"] = url
        captured["body"] = body
        return True, {"estimated_cost_cents": 11}

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._estimate_cost(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"agent_id": "agent_123", "input_payload": {"task": "review"}},
    )
    assert ok is True
    assert result["estimated_cost_cents"] == 11
    assert captured["url"] == "https://aztea.test/agents/agent_123/estimate"
    assert captured["body"] == {"task": "review"}


def test_list_recipes_uses_recipes_route(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_get(_session, url, _hdrs, _timeout, **_kwargs):
        captured["url"] = url
        return True, {"recipes": [{"pipeline_id": "review-and-test"}]}

    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    ok, result = meta_tools._list_recipes(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
    )
    assert ok is True
    assert captured["url"] == "https://aztea.test/recipes"
    assert result["count"] == 1


def test_list_pipelines_uses_pipelines_route(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_get(_session, url, _hdrs, _timeout, **_kwargs):
        captured["url"] = url
        return True, {"pipelines": [{"pipeline_id": "pipe_123"}]}

    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    ok, result = meta_tools._list_pipelines(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
    )
    assert ok is True
    assert captured["url"] == "https://aztea.test/pipelines"
    assert result["count"] == 1


def test_hire_async_accrues_caller_charge_cents(monkeypatch):
    session_state = {"spent_cents": 0, "budget_cents": None}

    def _fake_hire_async(_session, _base, _hdrs, _timeout, _arguments):
        return True, {"job_id": "job_123", "price_cents": 11, "caller_charge_cents": 14}

    monkeypatch.setattr(meta_tools, "_hire_async", _fake_hire_async)
    ok, result = meta_tools.call_meta_tool(
        session=None,
        base_url="https://aztea.test",
        api_key="az_test",
        tool_name="aztea_hire_async",
        arguments={"agent_id": "agent_123", "input_payload": {"task": "review"}},
        session_state=session_state,
        timeout=5,
    )
    assert ok is True
    assert result["caller_charge_cents"] == 14
    assert session_state["spent_cents"] == 14


def test_call_meta_tool_sends_client_header(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_wallet_balance(_session, _base, hdrs, _timeout):
        captured["headers"] = dict(hdrs)
        return True, {"balance_cents": 100}

    monkeypatch.setattr(meta_tools, "_wallet_balance", _fake_wallet_balance)
    ok, result = meta_tools.call_meta_tool(
        session=None,
        base_url="https://aztea.test",
        api_key="az_test",
        tool_name="aztea_wallet_balance",
        arguments={},
        session_state={"spent_cents": 0, "budget_cents": None},
        timeout=5,
    )
    assert ok is True
    assert result["balance_cents"] == 100
    assert captured["headers"]["X-Aztea-Version"] == "1.0"
    assert captured["headers"]["X-Aztea-Client"] == "claude-code"


def test_compare_agents_polls_until_complete(monkeypatch):
    calls = {"get": 0}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        assert url == "https://aztea.test/jobs/compare"
        assert body["agent_ids"] == ["agent_a", "agent_b"]
        return True, {"compare_id": "cmp_123", "status": "running", "total_charged_cents": 22}

    def _fake_get(_session, url, _hdrs, _timeout, **_kwargs):
        assert url == "https://aztea.test/jobs/compare/cmp_123"
        calls["get"] += 1
        if calls["get"] == 1:
            return True, {"compare_id": "cmp_123", "status": "running"}
        return True, {"compare_id": "cmp_123", "status": "complete", "jobs": [{"job_id": "job_1"}]}

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    monkeypatch.setattr(meta_tools.time, "sleep", lambda _seconds: None)
    ok, result = meta_tools._compare_agents(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"agent_ids": ["agent_a", "agent_b"], "input_payload": {"task": "compare"}, "wait_seconds": 5},
    )
    assert ok is True
    assert result["status"] == "complete"
    assert result["total_charged_cents"] == 22


def test_select_compare_winner_posts_selection(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["url"] = url
        captured["body"] = body
        return True, {"compare_id": "cmp_123", "winner_agent_id": "agent_a"}

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._select_compare_winner(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"compare_id": "cmp_123", "winner_agent_id": "agent_a"},
    )
    assert ok is True
    assert result["winner_agent_id"] == "agent_a"
    assert captured["url"] == "https://aztea.test/jobs/compare/cmp_123/select"
    assert captured["body"] == {"winner_agent_id": "agent_a"}


def test_compare_status_gets_existing_compare(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_get(_session, url, _hdrs, _timeout, **_kwargs):
        captured["url"] = url
        return True, {"compare_id": "cmp_123", "status": "running"}

    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    ok, result = meta_tools._compare_status(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"compare_id": "cmp_123"},
    )
    assert ok is True
    assert result["status"] == "running"
    assert captured["url"] == "https://aztea.test/jobs/compare/cmp_123"


def test_run_pipeline_polls_until_complete(monkeypatch):
    calls = {"get": 0}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        assert url == "https://aztea.test/pipelines/pipe_123/run"
        assert body == {"input_payload": {"task": "review"}}
        return True, {"pipeline_id": "pipe_123", "run_id": "run_123", "status": "running"}

    def _fake_get(_session, url, _hdrs, _timeout, **_kwargs):
        assert url == "https://aztea.test/pipelines/pipe_123/runs/run_123"
        calls["get"] += 1
        if calls["get"] == 1:
            return True, {"run_id": "run_123", "pipeline_id": "pipe_123", "status": "running"}
        return True, {
            "run_id": "run_123",
            "pipeline_id": "pipe_123",
            "status": "complete",
            "output_payload": {"summary": "done"},
            "step_results": {"review": {"summary": "done"}},
        }

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    monkeypatch.setattr(meta_tools.time, "sleep", lambda _seconds: None)
    ok, result = meta_tools._run_pipeline(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"pipeline_id": "pipe_123", "input_payload": {"task": "review"}, "wait_seconds": 5},
    )
    assert ok is True
    assert result["status"] == "complete"
    assert result["output_payload"] == {"summary": "done"}
    assert result["step_results"] == {"review": {"summary": "done"}}


def test_pipeline_status_gets_existing_run(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_get(_session, url, _hdrs, _timeout, **_kwargs):
        captured["url"] = url
        return True, {"run_id": "run_123", "pipeline_id": "pipe_123", "status": "running"}

    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    ok, result = meta_tools._pipeline_status(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"pipeline_id": "pipe_123", "run_id": "run_123"},
    )
    assert ok is True
    assert result["status"] == "running"
    assert captured["url"] == "https://aztea.test/pipelines/pipe_123/runs/run_123"


def test_run_recipe_uses_recipe_route_and_polls(monkeypatch):
    calls = {"get": 0}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        assert url == "https://aztea.test/recipes/review-and-test/run"
        assert body == {"input_payload": {"code": "print('hi')"}}
        return True, {
            "recipe_id": "review-and-test",
            "pipeline_id": "review-and-test",
            "run_id": "run_recipe_123",
            "status": "running",
        }

    def _fake_get(_session, url, _hdrs, _timeout, **_kwargs):
        assert url == "https://aztea.test/pipelines/review-and-test/runs/run_recipe_123"
        calls["get"] += 1
        if calls["get"] == 1:
            return True, {"run_id": "run_recipe_123", "pipeline_id": "review-and-test", "status": "running"}
        return True, {
            "run_id": "run_recipe_123",
            "pipeline_id": "review-and-test",
            "status": "complete",
            "output_payload": {"tests": "generated"},
        }

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    monkeypatch.setattr(meta_tools.time, "sleep", lambda _seconds: None)
    ok, result = meta_tools._run_recipe(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"recipe_id": "review-and-test", "input_payload": {"code": "print('hi')"}, "wait_seconds": 5},
    )
    assert ok is True
    assert result["recipe_id"] == "review-and-test"
    assert result["status"] == "complete"
    assert result["output_payload"] == {"tests": "generated"}
