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
