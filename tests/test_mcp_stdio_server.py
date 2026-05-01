from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "aztea_mcp_server.py"
_SPEC = importlib.util.spec_from_file_location("aztea_mcp_server", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("Failed to load aztea_mcp_server module for tests.")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class _DummyBridge:
    def tools(self):
        return []

    def call_tool(self, _tool_name: str, _arguments: dict):
        return True, {}


class _FakeStdin:
    def __init__(self, raw: bytes) -> None:
        self.buffer = io.BytesIO(raw)


def test_auth_tool_uses_snake_case_input_schema_key():
    assert "input_schema" in _MODULE._AUTH_TOOL
    assert "inputSchema" not in _MODULE._AUTH_TOOL


def test_read_message_rejects_invalid_content_length(monkeypatch):
    server = _MODULE.MCPStdioServer(bridge=_DummyBridge(), refresh_seconds=60)
    monkeypatch.setattr(_MODULE.sys, "stdin", _FakeStdin(b"Content-Length: abc\r\n\r\n{}"))
    with pytest.raises(ValueError, match="Invalid Content-Length"):
        server._read_message()


def test_registry_bridge_headers_include_client_id():
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    headers = bridge._headers()
    assert headers["X-Aztea-Version"] == "1.0"
    assert headers["X-Aztea-Client"] == "claude-code"


def test_registry_bridge_uses_lazy_tool_list_when_flag_enabled(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    bridge._entries = [
        {
            "agent_id": "agent-1",
            "tool_name": "python_code_executor",
            "tool": {
                "name": "python_code_executor",
                "description": "Execute Python snippets.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        }
    ]
    tools = bridge.tools()
    names = [tool["name"] for tool in tools]
    assert names == ["aztea_search", "aztea_describe", "aztea_call"]
    assert tools[0]["annotations"]["readOnlyHint"] is True
    assert tools[2]["annotations"]["readOnlyHint"] is False


def test_registry_bridge_lazy_search_and_describe(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    bridge._entries = [
        {
            "agent_id": "agent-1",
            "tool_name": "python_code_executor",
            "tool": {
                "name": "python_code_executor",
                "description": "Execute Python snippets.",
                "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}},
                "output_schema": {"type": "object"},
            },
            "catalog_metadata": {
                "category": "Code Execution",
                "tooling_kind": "sandbox_execution",
                "stability_tier": "stable",
                "codex_recommended": True,
                "short_use_cases": ["run a snippet"],
                "price_per_call_usd": 0.06,
                "success_rate": 0.97,
                "trust_score": 91,
                "avg_latency_ms": 800,
            },
        }
    ]

    ok, search = bridge.call_tool("aztea_search", {"query": "python snippets"})
    assert ok is True
    assert search["results"][0]["slug"] == "python_code_executor"
    assert search["results"][0]["category"] == "Code Execution"
    assert search["results"][0]["codex_recommended"] is True

    ok, described = bridge.call_tool("aztea_describe", {"slug": "python_code_executor"})
    assert ok is True
    assert described["input_schema"]["properties"]["code"]["type"] == "string"
    assert described["category"] == "Code Execution"
    assert described["codex_recommended"] is True


def test_registry_bridge_describe_accepts_agent_suffix_alias(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    bridge._entries = [
        {
            "agent_id": "agent-review",
            "tool_name": "code_review_agent",
            "tool": {
                "name": "code_review_agent",
                "description": "Review code and diffs for correctness.",
                "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}},
                "output_schema": {"type": "object"},
            },
            "catalog_metadata": {
                "name": "Code Review Agent",
                "category": "Code Review",
                "tooling_kind": "structured_review",
                "stability_tier": "stable",
                "codex_recommended": True,
                "short_use_cases": ["review a diff"],
                "price_per_call_usd": 0.01,
                "success_rate": 0.9,
                "trust_score": 60,
                "avg_latency_ms": 1200,
            },
        }
    ]

    ok, described = bridge.call_tool("aztea_describe", {"slug": "code_review"})
    assert ok is True
    assert described["slug"] == "code_review_agent"


def test_initialize_instructions_encourage_proactive_orchestration():
    server = _MODULE.MCPStdioServer(bridge=_DummyBridge(), refresh_seconds=60)
    instructions = server._initialize_result()["instructions"]
    assert "Do not wait for the user to explicitly tell you to use Aztea" in instructions
    assert "aztea_hire_batch" in instructions
    assert "aztea_hire_async + aztea_job_status" in instructions


def test_registry_bridge_lazy_search_returns_workflow_hints_for_parallel_tasks(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    ok, result = bridge.call_tool("aztea_search", {"query": "review many files in parallel with a budget", "limit": 5})
    assert ok is True
    hints = result.get("workflow_hints") or []
    assert any("aztea_hire_batch" in hint for hint in hints)
    assert any("aztea_set_session_budget" in hint for hint in hints)


def test_word_truncate_breaks_on_word_boundary():
    # Regression for the 2026-05-01 prod audit: "…code-level f", "…claude-code "
    long = "Use when the user wants live CVE data for a package and wants more"
    out = _MODULE._word_truncate(long, 30)
    assert out.endswith("…")
    head = out.rstrip("…").rstrip()
    # Last visible character must be the end of a complete word
    assert " " in long[: len(head) + 1]
    # No-op for short inputs
    assert _MODULE._word_truncate("short", 50) == "short"


def test_verb_rule_promotes_sql_explainer_for_explain_query():
    # Regression: db_sandbox previously outranked sql_explainer for "explain SQL".
    promoted = _MODULE._verb_rule_score("sql_explainer", ["explain", "sql", "query"])
    demoted = _MODULE._verb_rule_score("db_sandbox", ["explain", "sql", "query"])
    assert promoted > 0
    assert demoted < 0
    # Sandbox stays on top for "run SQL"
    run_promoted = _MODULE._verb_rule_score("db_sandbox", ["run", "sql", "query"])
    assert run_promoted > 0
    # Topic-only query (no verb) leaves both at zero
    assert _MODULE._verb_rule_score("db_sandbox", ["sql"]) == 0
    assert _MODULE._verb_rule_score("sql_explainer", ["sql"]) == 0


def test_describe_surfaces_output_schema_fields(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    bridge._entries = [
        {
            "agent_id": "lint",
            "tool_name": "linter_agent",
            "tool": {
                "name": "linter_agent",
                "description": "Lint Python.",
                "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
                "output_schema": {
                    "type": "object",
                    "properties": {"issues": {"type": "array"}, "clean": {"type": "boolean"}},
                    "required": ["issues", "clean"],
                },
            },
            "catalog_metadata": {"category": "Code Quality"},
        }
    ]
    ok, described = bridge.call_tool("aztea_describe", {"slug": "linter_agent"})
    assert ok is True
    # Pre-2026-05-01 audit: output_schema returned but never highlighted.
    assert set(described["output_fields"]) == {"issues", "clean"}
    assert described["output_required_fields"] == ["issues", "clean"]


def test_mcp_text_formatter_makes_search_results_readable():
    text = _MODULE._mcp_text_from_payload(
        {
            "query": "review many files",
            "results": [
                {
                    "slug": "aztea_hire_batch",
                    "name": "aztea_hire_batch",
                    "category": "Platform",
                    "price_per_call_usd": None,
                    "trust_score": None,
                    "success_rate": None,
                    "quality_summary": "Claude-ready | stable",
                    "best_for": ["parallel subtasks"],
                }
            ],
            "workflow_hints": ["This task looks parallelizable. Consider aztea_hire_batch for many independent subtasks."],
            "next_step": "Best match: aztea_hire_batch.",
        }
    )
    assert "Aztea matches for: review many files" in text
    assert "parallel subtasks" in text
    assert "Workflow hints:" in text
