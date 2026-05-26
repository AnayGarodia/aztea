from __future__ import annotations

import importlib.util
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest
import requests
import uvicorn

from core import auth
from core import cache as result_cache
from core import compare
from core import disputes
from core import jobs
from core import payments
from core import pipelines
from core import registry
from core import reputation
# 1.6.3: meta_tools moved from scripts/ to the in-package aztea.mcp.* tree.
import sys as _sys
from pathlib import Path as _Path
_SDK = str(_Path(__file__).resolve().parents[2] / "sdks" / "python-sdk")
if _SDK not in _sys.path:
    _sys.path.insert(0, _SDK)
from aztea.mcp import meta_tools  # noqa: E402
import server.application as server

from tests.integration.helpers import TEST_MASTER_KEY, _close_module_conn


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def buyer_surface_server(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-buyer-surfaces-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes, result_cache, compare, pipelines)
    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))

    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)

    port = _free_tcp_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    app_server = uvicorn.Server(config)
    app_server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=app_server.run, name="buyer-surface-server", daemon=True)
    thread.start()

    try:
        deadline = time.time() + 8
        while not app_server.started and thread.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        assert app_server.started, "uvicorn server did not start in time"
        yield f"http://127.0.0.1:{port}"
    finally:
        app_server.should_exit = True
        thread.join(timeout=5)
        for module in modules:
            _close_module_conn(module)
        for suffix in ("", "-shm", "-wal"):
            path = Path(f"{db_path}{suffix}")
            if path.exists():
                path.unlink()


def _register_user_via_http(base_url: str, *, prefix: str) -> dict:
    token = uuid.uuid4().hex[:8]
    payload = {
        "username": f"{prefix}-{token}",
        "email": f"{prefix}-{token}@example.com",
        "password": "password123",
    }
    response = requests.post(f"{base_url}/auth/register", json=payload, timeout=15)
    assert response.status_code == 201, response.text
    return response.json()


def _fund_wallet(base_url: str, raw_api_key: str, amount_cents: int) -> dict:
    wallet_resp = requests.get(
        f"{base_url}/wallets/me",
        headers={"Authorization": f"Bearer {raw_api_key}"},
        timeout=15,
    )
    assert wallet_resp.status_code == 200, wallet_resp.text
    wallet = wallet_resp.json()
    deposit_resp = requests.post(
        f"{base_url}/wallets/deposit",
        headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"},
        json={"wallet_id": wallet["wallet_id"], "amount_cents": amount_cents, "memo": "buyer surface smoke"},
        timeout=15,
    )
    assert deposit_resp.status_code == 200, deposit_resp.text
    return wallet


def _execute_platform_tool(
    *,
    manifest: dict,
    tool_name: str,
    arguments: dict,
    base_url: str,
    api_key: str,
    client_id: str,
) -> tuple[bool, dict]:
    lookup = manifest["tool_lookup"][tool_name]
    if lookup["kind"] == "meta_tool":
        session_state = {"budget_cents": None, "spent_cents": 0}
        previous_client_id = meta_tools._DEFAULT_CLIENT_ID
        meta_tools._DEFAULT_CLIENT_ID = client_id
        try:
            return meta_tools.call_meta_tool(
                tool_name,
                arguments,
                base_url=base_url,
                api_key=api_key,
                timeout=15,
                session=requests.Session(),
                session_state=session_state,
            )
        finally:
            meta_tools._DEFAULT_CLIENT_ID = previous_client_id

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Aztea-Version": "1.0",
        "X-Aztea-Client": client_id,
    }
    response = requests.post(
        f"{base_url}/registry/agents/{lookup['agent_id']}/call",
        headers=headers,
        json=arguments,
        timeout=20,
    )
    body = response.json()
    if not isinstance(body, dict):
        return response.ok, {"result": body}
    # Unwrap the standard sync call envelope so tests access agent output directly.
    if body.get("status") == "complete" and "output" in body:
        return response.ok, body["output"]
    return response.ok, body

def test_claude_stdio_mcp_smoke_lists_and_calls_control_plane_tool(buyer_surface_server):
    caller = _register_user_via_http(buyer_surface_server, prefix="claude-caller")
    # 1.6.3: the canonical MCP server module moved from scripts/ into the
    # SDK package (PR #38 consolidation). Use a real package import so the
    # relative imports inside `aztea.mcp.server` (`from . import manifest`)
    # resolve — `spec_from_file_location` can't establish the parent
    # package and breaks the file's relative imports.
    import sys
    _SDK = str(Path(__file__).resolve().parents[2] / "sdks" / "python-sdk")
    if _SDK not in sys.path:
        sys.path.insert(0, _SDK)
    module = importlib.import_module("aztea.mcp.server")

    old_client_id = module._DEFAULT_CLIENT_ID
    module._DEFAULT_CLIENT_ID = "claude-code"
    try:
        bridge = module.RegistryBridge(base_url=buyer_surface_server, api_key=str(caller["raw_api_key"]))
        bridge.refresh()
        server_obj = module.MCPStdioServer(bridge=bridge, refresh_seconds=60)

        init = server_obj._handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert init is not None
        assert init["result"]["serverInfo"]["name"] == "aztea-registry-mcp"

        listed = server_obj._handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert listed is not None
        tools = listed["result"]["tools"]
        names = {tool["name"] for tool in tools}
        # Phase 5.3 lazy MCP: 26+ per-agent tools collapsed to 3 surface tools.
        # Verb-first names are canonical; legacy aztea_* names still resolve via
        # the dispatch alias map.
        assert {"search_specialists", "describe_specialist", "call_specialist"} <= names

        # Hit the legacy alias path explicitly to prove backward compat.
        called = server_obj._handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "aztea_search", "arguments": {"query": "code"}},
            }
        )
        assert called is not None
        result = called["result"]
        assert result.get("isError") is not True
        structured = result["structuredContent"]
        assert structured["count"] >= 1

        # And the canonical verb-first name must produce a structurally
        # equivalent payload — this is the no-divergence contract.
        called_canonical = server_obj._handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "search_specialists", "arguments": {"query": "code"}},
            }
        )
        assert called_canonical is not None
        result_canonical = called_canonical["result"]
        assert result_canonical.get("isError") is not True
        structured_canonical = result_canonical["structuredContent"]
        assert structured_canonical["count"] == structured["count"]
        # Same shape, same key set: alias path and canonical path are
        # interchangeable from the model's perspective.
        assert set(structured_canonical.keys()) == set(structured.keys())
    finally:
        module._DEFAULT_CLIENT_ID = old_client_id


def test_codex_tool_manifest_supports_meta_and_registry_execution(buyer_surface_server):
    caller = _register_user_via_http(buyer_surface_server, prefix="codex-caller")
    _fund_wallet(buyer_surface_server, str(caller["raw_api_key"]), 500)

    headers = {
        "Authorization": f"Bearer {caller['raw_api_key']}",
        "X-Aztea-Version": "1.0",
        "X-Aztea-Client": "codex",
    }
    manifest_resp = requests.get(f"{buyer_surface_server}/codex/tools", headers=headers, timeout=15)
    assert manifest_resp.status_code == 200, manifest_resp.text
    manifest = manifest_resp.json()
    assert manifest["tool_lookup"]["aztea_list_recipes"]["kind"] == "meta_tool"

    ok_recipes, recipes = _execute_platform_tool(
        manifest=manifest,
        tool_name="aztea_list_recipes",
        arguments={},
        base_url=buyer_surface_server,
        api_key=str(caller["raw_api_key"]),
        client_id="codex",
    )
    assert ok_recipes is True
    # 2026-05-26 platform-pivot cull dropped two secret-scanner-fan-out recipes;
    # curated catalog is now {audit-deps, domain-health}.
    assert recipes["count"] >= 2

    ok_run, result = _execute_platform_tool(
        manifest=manifest,
        tool_name="python_code_executor",
        arguments={"code": "print(2 + 2)", "explain": False, "timeout": 3},
        base_url=buyer_surface_server,
        api_key=str(caller["raw_api_key"]),
        client_id="codex",
    )
    assert ok_run is True
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "4"


def test_gemini_tool_manifest_supports_meta_and_registry_execution(buyer_surface_server):
    caller = _register_user_via_http(buyer_surface_server, prefix="gemini-caller")
    _fund_wallet(buyer_surface_server, str(caller["raw_api_key"]), 500)

    headers = {
        "Authorization": f"Bearer {caller['raw_api_key']}",
        "X-Aztea-Version": "1.0",
        "X-Aztea-Client": "gemini-cli",
    }
    manifest_resp = requests.get(f"{buyer_surface_server}/gemini/tools", headers=headers, timeout=15)
    assert manifest_resp.status_code == 200, manifest_resp.text
    manifest = manifest_resp.json()
    declarations = {tool["name"] for tool in manifest["function_declarations"]}
    assert "aztea_list_recipes" in declarations

    ok_recipes, recipes = _execute_platform_tool(
        manifest=manifest,
        tool_name="aztea_list_recipes",
        arguments={},
        base_url=buyer_surface_server,
        api_key=str(caller["raw_api_key"]),
        client_id="gemini-cli",
    )
    assert ok_recipes is True
    assert recipes["count"] >= 2  # 2026-05-26 platform-pivot cull

    ok_run, result = _execute_platform_tool(
        manifest=manifest,
        tool_name="python_code_executor",
        arguments={"code": "print(6 * 7)", "explain": False, "timeout": 3},
        base_url=buyer_surface_server,
        api_key=str(caller["raw_api_key"]),
        client_id="gemini-cli",
    )
    assert ok_run is True
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "42"
