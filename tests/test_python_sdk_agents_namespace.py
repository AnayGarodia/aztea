# SPDX-License-Identifier: Apache-2.0
"""Wave 2 (2026-05-26): regression coverage for the new `client.agents.*` namespace.

# OWNS: contract assertions for the high-level Python SDK surface that mirrors
#       the TypeScript SDK shape (`@aztea/sdk`'s client.agents.call/list/describe).
# INVARIANTS:
#   - client.agents.call(...) and client.hire(...) MUST reach the same internal
#     impl with identical arguments — only the deprecation warning differs.
#   - client.hire(...) MUST emit exactly one DeprecationWarning per call.
#   - client.agents.list(owner_id=...) MUST pass owner_id through as a query
#     param so builder-profile pages can list "all agents by builder X".

These tests use unittest.mock for HTTP — no live server. The integration story
(SDK + real uvicorn) is covered by tests/test_python_sdk_consolidation.py;
keeping these lightweight means the namespace change has fast feedback for
the next agent (or human) who touches it.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Match the path pattern in test_python_sdk_consolidation.py so the SDK
# import resolves to the in-repo source, not a stale pip-installed copy.
SDK_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "sdks" / "python-sdk"
if str(SDK_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_PYTHON_ROOT))

from aztea import AzteaClient  # noqa: E402 — sys.path mutation above
from aztea._client_internals.namespaces import AgentsNamespace  # noqa: E402


def _make_client() -> AzteaClient:
    """Bare client — no requests session usage in these tests."""
    return AzteaClient(base_url="https://aztea.test", api_key="az_test")


# ─── Namespace plumbing ─────────────────────────────────────────────────────


def test_client_exposes_agents_namespace():
    client = _make_client()
    assert isinstance(client.agents, AgentsNamespace), (
        "Wave 2: client.agents must be an AgentsNamespace instance attached "
        "in AzteaClient.__init__"
    )


def test_agents_namespace_is_distinct_from_registry_namespace():
    """The two namespaces serve different needs — agents is the high-level
    'call this and get a result' surface; registry is the raw catalog
    REST wrapper. They MUST NOT be aliases of each other."""
    client = _make_client()
    assert client.agents is not client.registry


# ─── client.agents.call() ───────────────────────────────────────────────────


def test_agents_call_reaches_call_agent_impl_with_same_args(monkeypatch):
    """The agents.call entry point MUST delegate to _call_agent_impl with
    every kwarg forwarded unchanged. Any drift between agents.call's
    signature and _call_agent_impl is a silent contract break."""
    client = _make_client()
    spy = MagicMock(return_value="job-result-sentinel")
    monkeypatch.setattr(client, "_call_agent_impl", spy)

    result = client.agents.call(
        "secret_scanner",
        {"text": "AKIA..."},
        wait=False,
        timeout_seconds=42,
        max_attempts=2,
        budget_cents=500,
        callback_url="https://example.com/hook",
        callback_secret="sk_test",
    )

    assert result == "job-result-sentinel"
    spy.assert_called_once()
    args, kwargs = spy.call_args
    assert args == ("secret_scanner", {"text": "AKIA..."})
    assert kwargs["wait"] is False
    assert kwargs["timeout_seconds"] == 42
    assert kwargs["max_attempts"] == 2
    assert kwargs["budget_cents"] == 500
    assert kwargs["callback_url"] == "https://example.com/hook"
    assert kwargs["callback_secret"] == "sk_test"


def test_agents_call_does_NOT_emit_deprecation_warning(monkeypatch):
    """The new preferred surface must be warning-free. The legacy hire()
    path is where the DeprecationWarning lives."""
    client = _make_client()
    monkeypatch.setattr(client, "_call_agent_impl", MagicMock(return_value="ok"))
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        client.agents.call("secret_scanner", {"text": "x"})
    deprecations = [w for w in recorded if issubclass(w.category, DeprecationWarning)]
    assert deprecations == [], (
        f"client.agents.call() must not emit DeprecationWarning; got: "
        f"{[str(w.message) for w in deprecations]}"
    )


# ─── client.hire() deprecation ──────────────────────────────────────────────


def test_hire_emits_deprecation_warning_once_per_call(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "_call_agent_impl", MagicMock(return_value="ok"))
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        client.hire("secret_scanner", {"text": "x"})
    deprecations = [w for w in recorded if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    msg = str(deprecations[0].message)
    assert "client.agents.call" in msg, (
        "Deprecation message must point users at the replacement; got: " + msg
    )
    assert "hire" in msg


def test_hire_and_agents_call_reach_same_impl_with_same_args(monkeypatch):
    """End-to-end equivalence — calling hire(args) and agents.call(args)
    must invoke _call_agent_impl with the same positional+keyword shape."""
    client = _make_client()
    spy = MagicMock(return_value="ok")
    monkeypatch.setattr(client, "_call_agent_impl", spy)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        client.hire("scanner", {"x": 1}, timeout_seconds=30, max_attempts=4)
    client.agents.call("scanner", {"x": 1}, timeout_seconds=30, max_attempts=4)

    assert spy.call_count == 2
    first_args = spy.call_args_list[0]
    second_args = spy.call_args_list[1]
    assert first_args.args == second_args.args
    assert first_args.kwargs == second_args.kwargs


# ─── client.agents.list() ───────────────────────────────────────────────────


def test_agents_list_forwards_owner_id_filter(monkeypatch):
    """The new owner_id filter must reach the backend as a query param. The
    backend route (added in the same Wave 2 batch) reads ?owner_id=... on
    GET /registry/agents to power builder-profile pages."""
    client = _make_client()
    captured: dict = {}

    def _fake_request_json(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = kwargs.get("params") or {}
        return {"agents": []}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    client.agents.list(owner_id="user_abc123")

    assert captured["method"] == "GET"
    assert captured["path"] == "/registry/agents"
    assert captured["params"].get("owner_id") == "user_abc123"


def test_agents_list_omits_owner_id_when_not_supplied(monkeypatch):
    """Querying without owner_id must NOT add an empty owner_id= param,
    which would change the backend's interpretation from 'all' to 'none'."""
    client = _make_client()
    captured: dict = {}

    def _fake_request_json(method, path, **kwargs):
        captured["params"] = kwargs.get("params") or {}
        return {"agents": []}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    client.agents.list()

    assert "owner_id" not in captured["params"]


def test_agents_list_supports_tag_and_rank_by(monkeypatch):
    client = _make_client()
    captured: dict = {}

    def _fake_request_json(method, path, **kwargs):
        captured["params"] = kwargs.get("params") or {}
        return {"agents": []}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    client.agents.list(tag="security", rank_by="price")

    assert captured["params"]["tag"] == "security"
    assert captured["params"]["rank_by"] == "price"


# ─── client.agents.describe() ───────────────────────────────────────────────


def test_agents_describe_resolves_slug_before_fetch(monkeypatch):
    client = _make_client()

    monkeypatch.setattr(
        client, "_resolve_agent_reference",
        lambda ref: "00000000-0000-0000-0000-000000000001" if ref == "scanner" else ref,
    )
    seen: dict = {}

    def _fake_get_agent(agent_id):
        seen["agent_id"] = agent_id
        return MagicMock(spec=["agent_id", "name", "slug"], agent_id=agent_id, name="X", slug="scanner")

    monkeypatch.setattr(client, "get_agent", _fake_get_agent)
    client.agents.describe("scanner")
    assert seen["agent_id"] == "00000000-0000-0000-0000-000000000001"
