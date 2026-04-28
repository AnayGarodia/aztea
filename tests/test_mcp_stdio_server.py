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
        }
    ]

    ok, search = bridge.call_tool("aztea_search", {"query": "python snippets"})
    assert ok is True
    assert search["results"][0]["slug"] == "python_code_executor"

    ok, described = bridge.call_tool("aztea_describe", {"slug": "python_code_executor"})
    assert ok is True
    assert described["input_schema"]["properties"]["code"]["type"] == "string"
