"""
Regression tests — one test per bug fix.

Fix 1: _caller_from_raw_api_key used wrong auth function name
Fix 2: MCP manifest used camelCase keys and agentmarket__ prefix
Fix 3: get_agents() did not filter suspended agents
Fix 4: TrustGauge used raw success_rate instead of backend trust_score (frontend)
Fix 5: ApiKeyRow copied key_prefix instead of warning user (frontend)
Fix 6: Legacy unused components still present on disk
Fix 7: disputes.py duplicated the caller_ratings table definition
Fix 8: GET /runs lacked X-Skipped-Lines header for decode failures
"""

import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fix 1 — verify_agent_api_key is called, not the non-existent verify_agent_key
# ---------------------------------------------------------------------------

def test_fix1_auth_module_exposes_verify_agent_api_key_not_verify_agent_key():
    """The correct function name must exist; the old wrong name must not."""
    from core import auth
    assert callable(getattr(auth, "verify_agent_api_key", None)), (
        "auth.verify_agent_api_key must exist"
    )
    assert not hasattr(auth, "verify_agent_key"), (
        "auth.verify_agent_key is the old wrong name and must not exist"
    )


def test_fix1_server_calls_verify_agent_api_key(tmp_path):
    """The _caller_from_raw_api_key function in server.py must call verify_agent_api_key."""
    import inspect
    import server
    src = inspect.getsource(server._caller_from_raw_api_key)
    assert "verify_agent_api_key" in src, (
        "_caller_from_raw_api_key must call verify_agent_api_key"
    )
    assert "verify_agent_key(" not in src, (
        "_caller_from_raw_api_key must not call the old verify_agent_key"
    )


# ---------------------------------------------------------------------------
# Fix 2 — MCP manifest keys are snake_case and tool names have no prefix
# ---------------------------------------------------------------------------

def test_fix2_mcp_manifest_uses_snake_case_keys():
    from core import mcp_manifest
    agents = [
        {
            "agent_id": "aaaaaaaa-0000-0000-0000-000000000001",
            "name": "Test Agent",
            "description": "A test.",
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {"type": "object", "properties": {}},
        }
    ]
    entries = mcp_manifest.build_mcp_tool_entries(agents)
    tool = entries[0]["tool"]
    assert "input_schema" in tool,  "key must be input_schema (snake_case)"
    assert "output_schema" in tool, "key must be output_schema (snake_case)"
    assert "inputSchema" not in tool,  "camelCase inputSchema must not be present"
    assert "outputSchema" not in tool, "camelCase outputSchema must not be present"


def test_fix2_mcp_tool_names_have_no_prefix():
    from core import mcp_manifest
    agents = [
        {
            "agent_id": "aaaaaaaa-0000-0000-0000-000000000002",
            "name": "My Agent",
            "description": "desc",
            "input_schema": {},
            "output_schema": {},
        }
    ]
    entries = mcp_manifest.build_mcp_tool_entries(agents)
    name = entries[0]["tool_name"]
    assert not name.startswith("agentmarket__"), (
        f"tool name '{name}' must not have agentmarket__ prefix"
    )
    assert name == "my_agent", f"expected 'my_agent', got '{name}'"


# ---------------------------------------------------------------------------
# Fix 3 — get_agents() excludes suspended agents (not just banned)
# ---------------------------------------------------------------------------

@pytest.fixture()
def registry_db(tmp_path, monkeypatch):
    from core import registry, reputation, auth, payments, jobs, disputes
    db_path = str(tmp_path / "reg.db")

    def _close(module):
        conn = getattr(getattr(module, "_local", None), "conn", None)
        if conn:
            conn.close()
            try:
                delattr(module._local, "conn")
            except AttributeError:
                pass

    modules = (registry, reputation, auth, payments, jobs, disputes)
    for m in modules:
        _close(m)
        monkeypatch.setattr(m, "DB_PATH", db_path)

    # Stub out embeddings so registration doesn't need the model
    import numpy as np
    dim = registry.embeddings.EMBEDDING_DIM
    monkeypatch.setattr(registry.embeddings, "embed_text", lambda _: [0.0] * dim)

    registry.init_db()
    reputation.init_reputation_db()
    yield db_path

    for m in modules:
        _close(m)


def test_fix3_get_agents_excludes_suspended(registry_db, monkeypatch):
    from core import registry
    active_id = registry.register_agent(
        name="Active Agent", description="active", endpoint_url="https://example.com/a",
        price_per_call_usd=0.01, tags=[],
    )
    suspended_id = registry.register_agent(
        name="Suspended Agent", description="suspended", endpoint_url="https://example.com/s",
        price_per_call_usd=0.01, tags=[],
    )
    registry.set_agent_status(suspended_id, "suspended")

    agents = registry.get_agents()
    ids = {a["agent_id"] for a in agents}
    assert active_id in ids, "active agent should appear"
    assert suspended_id not in ids, "suspended agent must be excluded"


def test_fix3_get_agents_excludes_banned(registry_db, monkeypatch):
    from core import registry
    banned_id = registry.register_agent(
        name="Banned Agent", description="banned", endpoint_url="https://example.com/b",
        price_per_call_usd=0.01, tags=[],
    )
    registry.set_agent_status(banned_id, "banned")

    agents = registry.get_agents()
    ids = {a["agent_id"] for a in agents}
    assert banned_id not in ids, "banned agent must be excluded"


# ---------------------------------------------------------------------------
# Fix 4 & Fix 5 — frontend JSX changes; verified via source inspection
# ---------------------------------------------------------------------------

def test_fix4_trust_gauge_uses_trust_score_field():
    """TrustGauge.jsx must reference agent.trust_score, not agent.success_rate."""
    jsx = Path(__file__).resolve().parent.parent / "frontend/src/features/agents/TrustGauge.jsx"
    src = jsx.read_text()
    assert "trust_score" in src, "TrustGauge must use trust_score"
    assert "success_rate" not in src, "TrustGauge must not use raw success_rate"


def test_fix5_settings_page_warns_about_prefix_only():
    """SettingsPage must warn that only the key prefix is stored, not copy it silently."""
    jsx = Path(__file__).resolve().parent.parent / "frontend/src/pages/SettingsPage.jsx"
    src = jsx.read_text()
    assert "Only the prefix is stored" in src, (
        "SettingsPage must contain the prefix-only warning"
    )
    # The old buggy handleCopy wrote the prefix+ellipsis to the clipboard
    assert "key_prefix + '…'" not in src, (
        "ApiKeyRow must not copy the key prefix to the clipboard"
    )


# ---------------------------------------------------------------------------
# Fix 6 — legacy unused components must be deleted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", [
    "Dashboard.jsx",
    "CallWorkspace.jsx",
    "ActivityPanel.jsx",
    "RegisterAgentModal.jsx",
    "LandingPage.jsx",
])
def test_fix6_legacy_components_deleted(filename):
    path = (
        Path(__file__).resolve().parent.parent
        / "frontend/src/components"
        / filename
    )
    assert not path.exists(), (
        f"{filename} is a legacy unused component and must be deleted"
    )


# ---------------------------------------------------------------------------
# Fix 7 — disputes.py must NOT define the caller_ratings table
# ---------------------------------------------------------------------------

def test_fix7_disputes_does_not_define_caller_ratings_table():
    """disputes.py must not contain a CREATE TABLE … caller_ratings block."""
    src = (Path(__file__).resolve().parent.parent / "core/disputes.py").read_text()
    # Allow only comments/mentions; reject the DDL
    assert "CREATE TABLE IF NOT EXISTS caller_ratings" not in src, (
        "caller_ratings must only be defined in reputation.py"
    )


def test_fix7_caller_ratings_defined_in_reputation():
    src = (Path(__file__).resolve().parent.parent / "core/reputation.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS caller_ratings" in src, (
        "caller_ratings canonical definition must remain in reputation.py"
    )


# ---------------------------------------------------------------------------
# Fix 8 — GET /runs returns X-Skipped-Lines header for malformed JSON lines
# ---------------------------------------------------------------------------

def test_fix8_runs_endpoint_emits_skipped_lines_header(tmp_path, monkeypatch):
    """When runs.jsonl contains invalid JSON lines the header must count them."""
    import server
    from fastapi.testclient import TestClient

    runs_file = tmp_path / "runs.jsonl"
    runs_file.write_text(
        '{"id":"r1","status":"ok"}\n'
        'not-valid-json\n'
        '{"id":"r2","status":"ok"}\n'
        'also bad\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        server.os.path, "dirname",
        lambda _: str(tmp_path),
    )

    master_key = "test-skip-header-key"
    monkeypatch.setattr(server, "_MASTER_KEY", master_key)

    client = TestClient(server.app, raise_server_exceptions=False)
    resp = client.get(
        "/runs",
        headers={"Authorization": f"Bearer {master_key}"},
    )
    assert resp.status_code == 200
    assert "x-skipped-lines" in {k.lower() for k in resp.headers}
    assert resp.headers.get("x-skipped-lines") == "2", (
        f"expected 2 skipped lines, got {resp.headers.get('x-skipped-lines')}"
    )
    body = resp.json()
    assert body["skipped_lines"] == 2
    assert body["skipped_line_numbers"] == [2, 4]
