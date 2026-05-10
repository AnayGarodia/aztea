"""End-to-end logic test for the MCP-side workspace attach helper.

These tests exercise the consent state machine through the actual
``_attach_workspace_context`` function from ``scripts/aztea_mcp_server.py``,
without spinning up a real MCP server.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from core import workspace_consent as wc


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AZTEA_HOME", str(tmp_path / ".aztea"))
    return tmp_path


@pytest.fixture
def mcp_server_module():
    """Import the MCP server module fresh to pick up the test env.

    1.6.3: the canonical module moved from ``scripts.aztea_mcp_server``
    into the SDK at ``aztea.mcp.server``. Path-injection so the fixture
    works whether or not the SDK is pip-installed in the test venv.
    """
    import sys
    from pathlib import Path
    _SDK = str(Path(__file__).resolve().parents[1] / "sdks" / "python-sdk")
    if _SDK not in sys.path:
        sys.path.insert(0, _SDK)
    module = importlib.import_module("aztea.mcp.server")
    return module


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    (proj / "README.md").write_text("# Demo\n", encoding="utf-8")
    return proj


def test_unknown_consent_returns_notice_and_attaches_nothing(
    mcp_server_module, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    body: dict = {}
    notice = mcp_server_module._attach_workspace_context(body)
    assert notice is not None
    assert "workspace approve" in notice.lower()
    assert "workspace_context" not in body


def test_approved_consent_attaches_bundle_no_notice(
    mcp_server_module, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    wc.approve(project)
    body: dict = {}
    notice = mcp_server_module._attach_workspace_context(body)
    assert notice is None
    assert "workspace_context" in body
    assert body["workspace_context"]["cwd_basename"] == project.name
    assert "package.json" in body["workspace_context"]["manifests"]


def test_denied_consent_attaches_nothing_no_notice(
    mcp_server_module, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    wc.deny(project)
    body: dict = {}
    notice = mcp_server_module._attach_workspace_context(body)
    assert notice is None
    assert "workspace_context" not in body


def test_disable_env_var_short_circuits(
    mcp_server_module, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    wc.approve(project)
    monkeypatch.setenv("AZTEA_DISABLE_WORKSPACE_CONTEXT", "1")
    body: dict = {}
    assert mcp_server_module._attach_workspace_context(body) is None
    assert "workspace_context" not in body


def test_inner_input_also_receives_bundle(
    mcp_server_module, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """call_specialist passes the bundle into the agent payload directly."""
    monkeypatch.chdir(project)
    wc.approve(project)
    body: dict = {}
    inner: dict = {}
    mcp_server_module._attach_workspace_context(body, inner)
    assert "workspace_context" in body
    assert "workspace_context" in inner
    assert body["workspace_context"]["fingerprint"] == inner["workspace_context"]["fingerprint"]
