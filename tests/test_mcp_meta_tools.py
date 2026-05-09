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


def test_parse_strips_pydantic_help_urls():
    ok, result = meta_tools._parse(
        _FakeResponse(
            ok=False,
            status_code=422,
            payload={
                "detail": {
                    "message": "Input should be a valid string\nFor further information visit https://errors.pydantic.dev/2.13/v/string_type",
                }
            },
        )
    )
    assert ok is False
    assert "errors.pydantic.dev" not in result["message"]


def test_meta_tool_catalog_exposes_claude_control_plane():
    tool_names = {tool["name"] for tool in meta_tools.get_meta_tools()}
    assert {
        "aztea_estimate_cost",
        "aztea_hire_async",
        "aztea_job_status",
        "aztea_batch_status",
        "aztea_compare_agents",
        "aztea_compare_status",
        "aztea_list_recipes",
        "aztea_run_recipe",
        "aztea_list_pipelines",
        "aztea_run_pipeline",
        "aztea_pipeline_status",
    } <= tool_names


def test_meta_tools_include_annotations_for_permission_and_parallel_hints():
    by_name = {tool["name"]: tool for tool in meta_tools.get_meta_tools()}
    assert by_name["aztea_job_status"]["annotations"]["readOnlyHint"] is True
    assert by_name["aztea_hire_async"]["annotations"]["readOnlyHint"] is False


def test_set_session_budget_requires_explicit_budget_cents():
    ok, result = meta_tools.call_meta_tool(
        session=None,
        base_url="https://aztea.test",
        api_key="az_test",
        tool_name="aztea_set_session_budget",
        arguments={},
        session_state={"spent_cents": 0, "budget_cents": 500},
        timeout=5,
    )
    assert ok is False
    assert result["error"] == "INVALID_INPUT"
    assert "budget_cents is required" in result["message"]


def test_set_session_budget_rejects_unknown_keys():
    ok, result = meta_tools.call_meta_tool(
        session=None,
        base_url="https://aztea.test",
        api_key="az_test",
        tool_name="aztea_set_session_budget",
        arguments={"limit_cents": 200},
        session_state={"spent_cents": 0, "budget_cents": 500},
        timeout=5,
    )
    assert ok is False
    assert result["error"] == "INVALID_INPUT"
    assert result["allowed_fields"] == ["budget_cents"]


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


def test_wallet_balance_compacts_transaction_history(monkeypatch):
    txs = [{"tx_id": f"tx_{idx}"} for idx in range(8)]

    def _fake_wallet_balance(_session, _base, _hdrs, _timeout):
        return True, {"balance_cents": 1000, "transactions": txs}

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
    assert "transactions" not in result
    assert len(result["recent_transactions"]) == 5
    assert result["transaction_count"] == 8
    assert result["transactions_omitted"] == 3


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


def test_compare_agents_accepts_slugs_and_input_alias(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_resolve(_session, _base, _hdrs, _timeout, args):
        return f"agent-{args['slug']}", None

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["url"] = url
        captured["body"] = body
        return True, {"compare_id": "cmp_123", "status": "pending"}

    def _fake_get(_session, url, _hdrs, _timeout, **_kwargs):
        captured["status_url"] = url
        return True, {"compare_id": "cmp_123", "status": "complete"}

    monkeypatch.setattr(meta_tools, "_resolve_agent_id", _fake_resolve)
    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    ok, result = meta_tools._compare_agents(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"slugs": ["lint_a", "lint_b"], "input": {"task": "compare"}},
    )
    assert ok is True
    assert result["compare_id"] == "cmp_123"
    assert captured["url"] == "https://aztea.test/jobs/compare"
    assert captured["status_url"] == "https://aztea.test/jobs/compare/cmp_123"
    assert captured["body"]["agent_ids"] == ["agent-lint_a", "agent-lint_b"]
    assert captured["body"]["input_payload"] == {"task": "compare"}


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


def test_run_recipe_accrues_pipeline_step_charges(monkeypatch):
    session_state = {"spent_cents": 0, "budget_cents": None}

    def _fake_run_recipe(_session, _base, _hdrs, _timeout, _arguments):
        return True, {
            "recipe_id": "audit-deps",
            "status": "complete",
            "step_results": {"audit": {"caller_charge_cents": 4, "summary": "done"}},
        }

    monkeypatch.setattr(meta_tools, "_run_recipe", _fake_run_recipe)
    ok, result = meta_tools.call_meta_tool(
        session=None,
        base_url="https://aztea.test",
        api_key="az_test",
        tool_name="aztea_run_recipe",
        arguments={"recipe_id": "audit-deps", "input_payload": {"manifest": "{}"}},
        session_state=session_state,
        timeout=5,
    )
    assert ok is True
    assert result["status"] == "complete"
    assert session_state["spent_cents"] == 4


def test_batch_status_polls_many_jobs(monkeypatch):
    def _fake_job_status(_session, _base, _hdrs, _timeout, args):
        return True, {"job_id": args["job_id"], "status": "complete", "messages": []}

    monkeypatch.setattr(meta_tools, "_job_status", _fake_job_status)
    ok, result = meta_tools._batch_status(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"job_ids": ["job_1", "job_2"]},
    )
    assert ok is True
    assert result["complete_count"] == 2
    assert [job["job_id"] for job in result["jobs"]] == ["job_1", "job_2"]


def test_batch_status_prefers_batch_id(monkeypatch):
    captured = {}

    def _fake_get(_session, url, _hdrs, _timeout, params=None):
        captured["url"] = url
        captured["params"] = params
        return True, {"batch_id": "batch_1", "parallel_hire_trace": {"jobs": []}}

    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    ok, result = meta_tools._batch_status(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"batch_id": "batch_1"},
    )
    assert ok is True
    assert captured["url"] == "https://aztea.test/jobs/batch/batch_1"
    assert result["batch_id"] == "batch_1"
    assert "parallel_hire_trace" in result


def test_hire_batch_accepts_slugs_and_total_cap(monkeypatch):
    def _fake_resolve(_session, _base, _hdrs, _timeout, args):
        return f"agent-{args['slug']}", None

    captured = {}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["url"] = url
        captured["body"] = body
        return True, {
            "batch_id": "batch_1",
            "jobs": [{"job_id": "job_1"}],
            "total_charged_cents": 4,
        }

    monkeypatch.setattr(meta_tools, "_resolve_agent_id", _fake_resolve)
    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._hire_batch(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={
            "intent": "check two files",
            "max_total_cents": 25,
            "jobs": [{"slug": "linter_agent", "input_payload": {"code": "x=1"}}],
        },
    )
    assert ok is True
    assert captured["url"] == "https://aztea.test/jobs/batch"
    assert captured["body"]["intent"] == "check two files"
    assert captured["body"]["max_total_cents"] == 25
    assert captured["body"]["jobs"][0]["agent_id"] == "agent-linter_agent"
    assert result["job_ids"] == ["job_1"]
    assert "parallel marketplace hire" in result["note"].lower()


def test_discover_filters_toy_and_low_relevance_intent_matches(monkeypatch):
    def _fake_post(_session, _url, _hdrs, _timeout, _body):
        return True, {
            "results": [
                {"agent": {"agent_id": "wiki", "slug": "wiki_research", "name": "Wikipedia Research", "description": "Look up wiki articles"}},
                {"agent": {"agent_id": "lint", "slug": "linter_agent", "name": "Linter Agent", "description": "Lint code"}},
                {"agent": {"agent_id": "img", "slug": "image_generator", "name": "Image Generator", "description": "Generate images"}},
            ]
        }

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._discover(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"query": "generate an image", "limit": 5},
    )
    assert ok is True
    assert result["count"] == 1
    assert result["results"][0]["slug"] == "image_generator"


def test_word_truncate_breaks_on_word_boundary():
    # Regression for the 2026-05-01 audit: "…code-level f", "…claude-code "
    text = "Use when the user wants live CVE data for a package"
    out = meta_tools._word_truncate(text, 30)
    assert out.endswith("…")
    # Should never end mid-word
    assert not out.rstrip("…").endswith(" ")
    head = out.rstrip("…").rstrip()
    assert " " in text[: len(head) + 1]
    # Short input passes through unchanged
    assert meta_tools._word_truncate("short", 50) == "short"


def test_resolve_agent_id_prefers_explicit_uuid():
    agent_id, err = meta_tools._resolve_agent_id(
        session=None, base="https://aztea.test", hdrs={}, timeout=5,
        args={"agent_id": "11111111-2222-3333-4444-555555555555", "slug": "ignored"},
    )
    assert err is None
    assert agent_id == "11111111-2222-3333-4444-555555555555"


def test_resolve_agent_id_falls_back_to_slug(monkeypatch):
    def _fake_post(_session, url, _hdrs, _timeout, body):
        assert url.endswith("/registry/search")
        # Increased to 50 (was 5) to defend against typo'd slugs that rank
        # outside the top 5 in semantic search. Money-routing must never
        # silently land on a similarly-named agent.
        assert body == {"query": "linter_agent", "limit": 50}
        return True, {
            "results": [
                {"agent": {"agent_id": "aaa", "slug": "code_review_agent"}},
                {"agent": {"agent_id": "bbb", "slug": "linter_agent"}},
            ]
        }

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    agent_id, err = meta_tools._resolve_agent_id(
        session=None, base="https://aztea.test", hdrs={}, timeout=5,
        args={"slug": "linter_agent"},
    )
    assert err is None
    assert agent_id == "bbb"


def test_resolve_agent_id_returns_error_when_neither_provided():
    agent_id, err = meta_tools._resolve_agent_id(
        session=None, base="https://aztea.test", hdrs={}, timeout=5, args={},
    )
    assert agent_id == ""
    assert err is not None
    assert err["error"] == "INVALID_INPUT"


def test_cancel_job_posts_to_cancel_route(monkeypatch):
    captured = {}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["url"] = url
        captured["body"] = body
        return True, {"job_id": "job_1", "status": "failed", "refund_amount_cents": 5}

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._cancel_job(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"job_id": "job_1", "reason": "duplicate submission"},
    )
    assert ok is True
    assert captured["url"] == "https://aztea.test/jobs/job_1/cancel"
    assert captured["body"]["reason"] == "duplicate submission"
    assert "note" in result


def test_cancel_job_rejects_missing_job_id():
    ok, result = meta_tools._cancel_job(
        session=None, base="https://aztea.test", hdrs={}, timeout=5, args={},
    )
    assert ok is False
    assert result["error"] == "INVALID_INPUT"


def test_get_examples_accepts_slug(monkeypatch):
    posted: dict = {}
    fetched: dict = {}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        posted["url"] = url
        posted["body"] = body
        return True, {"results": [{"agent": {"agent_id": "agent-uuid", "slug": "linter_agent"}}]}

    def _fake_get(_session, url, _hdrs, _timeout):
        fetched["url"] = url
        return True, {"name": "Linter", "output_examples": []}

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    monkeypatch.setattr(meta_tools, "_get", _fake_get)

    ok, result = meta_tools._get_examples(
        session=None, base="https://aztea.test", hdrs={}, timeout=5,
        args={"slug": "linter_agent"},
    )
    assert ok is True
    assert posted["url"].endswith("/registry/search")
    assert fetched["url"].endswith("/registry/agents/agent-uuid")
    assert result["agent_id"] == "agent-uuid"


def test_data_retention_policy_returns_privacy_summary(monkeypatch):
    def _fake_post(_session, url, _hdrs, _timeout, body):
        # _resolve_agent_id will hit /registry/search when only slug is given; we
        # bypass that by passing agent_id directly so this fixture is never called.
        raise AssertionError(f"unexpected POST {url} {body}")

    def _fake_get(_session, url, _hdrs, _timeout):
        assert url.endswith("/registry/agents/agent-uuid")
        return True, {
            "agent_id": "agent-uuid",
            "name": "Secret Scanner",
            "category": "Security",
            "examples_sensitive": True,
            "pii_safe": True,
            "outputs_not_stored": False,
            "audit_logged": True,
            "region_locked": None,
        }

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    ok, result = meta_tools._data_retention_policy(
        session=None, base="https://aztea.test", hdrs={}, timeout=5,
        args={"agent_id": "agent-uuid"},
    )
    assert ok is True
    assert result["publishes_work_examples"] is False
    assert result["pii_safe"] is True
    assert result["category"] == "Security"
    assert "does not publish work examples" in result["summary"]


def test_verify_job_signature_returns_unverified_on_missing_signature(monkeypatch):
    def _fake_get(_session, url, _hdrs, _timeout):
        # Pretend the signature endpoint 404s
        return False, {"error": "JOB_NOT_FOUND", "message": "no such job"}

    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    ok, result = meta_tools._verify_job_signature(
        session=None, base="https://aztea.test", hdrs={}, timeout=5,
        args={"job_id": "job-x"},
    )
    assert ok is False
    assert result["verified"] is False
    assert "signature unavailable" in result["verification_error"]


def test_cancel_job_is_in_meta_tool_names_and_schema():
    assert "aztea_cancel_job" in meta_tools.META_TOOL_NAMES
    tools = {t["name"]: t for t in meta_tools.get_meta_tools()}
    assert "aztea_cancel_job" in tools
    schema = tools["aztea_cancel_job"]["input_schema"]
    assert "job_id" in schema["properties"]
    assert schema["required"] == ["job_id"]


# ── Resource-grouped tools (aztea_job / aztea_budget / aztea_workflow) ─────


def test_grouped_tools_listed_first_in_get_meta_tools():
    tools = meta_tools.get_meta_tools()
    first_three_names = [t["name"] for t in tools[:3]]
    assert set(first_three_names) == {"aztea_job", "aztea_budget", "aztea_workflow"}


def test_always_visible_returns_only_three_grouped_tools():
    visible = meta_tools.always_visible_tools()
    names = sorted(t["name"] for t in visible)
    assert names == ["aztea_budget", "aztea_job", "aztea_workflow"]


def test_grouped_tool_names_in_meta_tool_names():
    for name in ("aztea_job", "aztea_budget", "aztea_workflow"):
        assert name in meta_tools.META_TOOL_NAMES


def test_grouped_dispatch_routes_rate_action_to_underlying(monkeypatch):
    """aztea_job(action='rate', ...) must dispatch to aztea_rate_job and strip
    `action` from the args before invoking it."""
    captured: dict[str, object] = {}

    real_call = meta_tools.call_meta_tool

    def _spy(tool_name, arguments, **kwargs):
        # Record the inner call (after action stripping) and short-circuit.
        if tool_name == "aztea_rate_job":
            captured["tool_name"] = tool_name
            captured["arguments"] = dict(arguments)
            return True, {"ok": True}
        # Fall through to real dispatcher for the grouped wrapper itself.
        return real_call(tool_name, arguments, **kwargs)

    monkeypatch.setattr(meta_tools, "call_meta_tool", _spy)
    ok, _ = meta_tools.call_meta_tool(
        "aztea_job",
        {"action": "rate", "job_id": "job_42", "rating": 5, "comment": "great"},
        base_url="https://aztea.test",
        api_key="key",
        timeout=5,
        session=None,
        session_state={},
    )
    assert ok is True
    assert captured["tool_name"] == "aztea_rate_job"
    args = captured["arguments"]
    assert "action" not in args  # stripped before dispatch
    assert args["job_id"] == "job_42"
    assert args["rating"] == 5
    assert args["comment"] == "great"


def test_grouped_dispatch_rejects_unknown_action():
    ok, result = meta_tools.call_meta_tool(
        "aztea_workflow",
        {"action": "teleport"},
        base_url="https://aztea.test",
        api_key="key",
        timeout=5,
        session=None,
        session_state={},
    )
    assert ok is False
    assert result.get("error") == "INVALID_INPUT"
    assert "allowed_actions" in result
    assert "hire_async" in result["allowed_actions"]


def test_grouped_dispatch_requires_action():
    ok, result = meta_tools.call_meta_tool(
        "aztea_budget",
        {},
        base_url="https://aztea.test",
        api_key="key",
        timeout=5,
        session=None,
        session_state={},
    )
    assert ok is False
    assert result.get("error") == "INVALID_INPUT"
    assert "balance" in result["allowed_actions"]


def test_hire_async_accepts_input_alias(monkeypatch):
    def _fake_resolve(_session, _base, _hdrs, _timeout, args):
        return f"agent-{args.get('slug') or args.get('agent_id')}", None

    captured: dict[str, object] = {}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["url"] = url
        captured["body"] = body
        return True, {"job_id": "job_async_1", "status": "pending"}

    monkeypatch.setattr(meta_tools, "_resolve_agent_id", _fake_resolve)
    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._hire_async(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"slug": "linter_agent", "input": {"language": "python", "code": "x=1\n"}},
    )
    assert ok is True
    assert captured["body"]["input_payload"] == {"language": "python", "code": "x=1\n"}
    assert result["job_id"] == "job_async_1"


def test_hire_batch_accepts_input_alias_and_dry_run(monkeypatch):
    def _fake_resolve(_session, _base, _hdrs, _timeout, args):
        return f"agent-{args['slug']}", None

    captured: dict[str, object] = {}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["url"] = url
        captured["body"] = body
        return True, {
            "mode": "parallel_marketplace_hire_estimate",
            "charge_status": "not_charged",
            "estimated_total_charged_cents": 2,
        }

    monkeypatch.setattr(meta_tools, "_resolve_agent_id", _fake_resolve)
    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._hire_batch(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={
            "intent": "preview only",
            "dry_run": True,
            "jobs": [
                {"slug": "linter_agent", "input": {"language": "python", "code": "x=1"}},
                {"slug": "secret_scanner", "input": {"content": "ghp_FAKE"}},
            ],
        },
    )
    assert ok is True
    assert captured["body"]["dry_run"] is True
    assert captured["body"]["jobs"][0]["input_payload"]["code"] == "x=1"
    assert captured["body"]["jobs"][1]["input_payload"]["content"] == "ghp_FAKE"
    assert result["charge_status"] == "not_charged"
    assert "claude_summary_hint" not in result


def test_global_retention_no_slug_returns_default_policy():
    ok, result = meta_tools._data_retention_policy(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={},
    )
    assert ok is True
    assert result["scope"] == "global"
    assert result["private_task_supported"] is True
    assert "private_task=true" in result["default_policy"]


def test_estimate_cost_accepts_input_alias(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_resolve(_session, _base, _hdrs, _timeout, args):
        return args["agent_id"], None

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["body"] = body
        return True, {"estimated_cost_cents": 5}

    monkeypatch.setattr(meta_tools, "_resolve_agent_id", _fake_resolve)
    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._estimate_cost(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"agent_id": "agent_1", "input": {"task": "x"}},
    )
    assert ok is True
    assert captured["body"] == {"task": "x"}
    assert result["estimated_cost_cents"] == 5


def test_batch_status_uses_compact_include_param(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_get(_session, url, _hdrs, _timeout, params=None):
        captured["url"] = url
        captured["params"] = params
        return True, {"batch_id": "batch_X"}

    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    ok, _result = meta_tools._batch_status(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"batch_id": "batch_X"},
    )
    assert ok is True
    assert captured["params"] == {"include": "minimal"}


def test_job_full_output_fetches_untruncated_payload(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_get(_session, url, _hdrs, _timeout, **_kwargs):
        captured["url"] = url
        return True, {"job_id": "job_1", "output_payload": {"large": True}}

    monkeypatch.setattr(meta_tools, "_get", _fake_get)
    ok, result = meta_tools._job_full_output(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"job_id": "job_1"},
    )
    assert ok is True
    assert captured["url"] == "https://aztea.test/jobs/job_1/full"
    assert result["output_payload"]["large"] is True


def test_clarify_accepts_response_alias(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_post(_session, url, _hdrs, _timeout, body):
        captured["url"] = url
        captured["body"] = body
        return True, {"message_id": 10}

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._clarify(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={
            "job_id": "job_1",
            "response": "Please review security only.",
            "request_message_id": 7,
        },
    )
    assert ok is True
    assert result["message_id"] == 10
    assert captured["body"]["payload"]["answer"] == "Please review security only."


def test_budget_estimate_requires_slug_or_agent_id_with_helpful_error():
    ok, result = meta_tools.call_meta_tool(
        "aztea_budget",
        {"action": "estimate", "input": {"task": "x"}},
        base_url="https://aztea.test",
        api_key="key",
        timeout=5,
        session=None,
        session_state={},
    )
    assert ok is False
    assert result["error"] == "INVALID_INPUT"
    assert "slug" in result["message"]
    assert result["required_one_of"] == ["slug", "agent_id"]
    # Accept either the verb-first canonical name or the legacy alias —
    # the rename ships in v0.2.0 but the dispatch keeps both working.
    assert ("search_specialists" in result["next_step"]
            or "aztea_search" in result["next_step"])


def test_discover_includes_input_and_pricing_metadata(monkeypatch):
    def _fake_post(_session, _url, _hdrs, _timeout, _body):
        return True, {
            "results": [
                {
                    "agent": {
                        "agent_id": "dep",
                        "slug": "dependency_auditor",
                        "name": "Dependency Auditor",
                        "description": "Audit deps for CVEs",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "manifest": {"type": "string"},
                                "ecosystem": {"type": "string"},
                            },
                            "required": ["manifest"],
                        },
                        "pricing_model": "per_call",
                        "pricing_config": {"per_call_cents": 1},
                    }
                }
            ]
        }

    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, result = meta_tools._discover(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={"query": "audit dependencies for CVEs", "limit": 5},
    )
    assert ok is True
    item = result["results"][0]
    assert item["required_fields"] == ["manifest"]
    assert "manifest" in item["input_fields"]
    assert "ecosystem" in item["input_fields"]
    assert item["pricing_model"] == "per_call"
