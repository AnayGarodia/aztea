"""Unit tests for the in-REPL register modal — submit + error mapping.

Covers the network-failure UX fix: when the registration HTTP call
times out or fails to connect, the user should see actionable inline
copy instead of a raw ``HTTPSConnectionPool(...)`` message. Also
covers the sync-fallback path of ``_submit`` (used when no asyncio
loop is running, e.g. inside tests) so the existing submission
behavior is preserved.
"""
from __future__ import annotations

import pytest

# The REPL package pulls in prompt_toolkit on import (register_modal
# builds a Float container at module load). prompt_toolkit is a CLI
# extra, not a core dep — when CI runs the base test job without it,
# skip cleanly instead of erroring at collection time.
pytest.importorskip("prompt_toolkit")

from aztea.cli.repl import register_modal as rm  # noqa: E402


class _FakeAuthApi:
    def __init__(self, *, raise_exc: Exception | None = None, result: dict | None = None):
        self._raise_exc = raise_exc
        self._result = result or {}

    def register(self, *, username: str, email: str, password: str) -> dict:
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


class _FakeClient:
    def __init__(self, auth: _FakeAuthApi):
        self.auth = auth

    def __init_signature_marker__(self):
        pass


def _install_fake_client(monkeypatch, *, raise_exc=None, result=None):
    """Patch AzteaClient so _perform_register_call uses our fake."""
    auth = _FakeAuthApi(raise_exc=raise_exc, result=result)

    def _factory(*args, **kwargs):
        return _FakeClient(auth=auth)

    monkeypatch.setattr("aztea.client.AzteaClient", _factory)


def test_perform_register_call_returns_ok_on_success(monkeypatch) -> None:
    _install_fake_client(monkeypatch, result={"raw_api_key": "az_test_abc", "username": "u"})
    outcome = rm._perform_register_call("user1", "u@example.com", "pw12345a")
    assert outcome["ok"] is True
    assert outcome["result"]["raw_api_key"] == "az_test_abc"


def test_perform_register_call_returns_exc_on_failure(monkeypatch) -> None:
    boom = RuntimeError("boom")
    _install_fake_client(monkeypatch, raise_exc=boom)
    outcome = rm._perform_register_call("user1", "u@example.com", "pw12345a")
    assert outcome["ok"] is False
    assert outcome["exc"] is boom


def test_surface_register_error_routes_through_shared_helper(capsys) -> None:
    """Network detection lives in cli.output.render_network_error; the
    register modal must delegate to it so all surfaces agree."""

    class ReadTimeout(Exception):
        pass

    rm._surface_register_error(ReadTimeout("HTTPSConnectionPool: Read timed out."))
    out = capsys.readouterr()
    text = out.out + out.err
    # The shared helper renders the same copy seen elsewhere — we just
    # check the namespaced code reaches the panel.
    assert "register.timeout" in text


def test_surface_register_error_maps_timeout_to_friendly_code(capsys) -> None:
    class ReadTimeout(Exception):
        pass

    rm._surface_register_error(ReadTimeout("HTTPSConnectionPool: Read timed out. (read timeout=30.0)"))
    out = capsys.readouterr()
    text = out.out + out.err
    assert "register.timeout" in text
    assert "took too long" in text.lower()
    # The raw urllib3 message must NOT leak through.
    assert "HTTPSConnectionPool" not in text


def test_surface_register_error_maps_connection_error(capsys) -> None:
    class ConnectionError(Exception):
        pass

    rm._surface_register_error(ConnectionError("Failed to establish a new connection"))
    out = capsys.readouterr()
    text = out.out + out.err
    assert "register.network" in text
    assert "couldn't reach" in text.lower()


def test_render_register_outcome_no_raw_key_emits_specific_code() -> None:
    text = rm._render_register_outcome(
        username="u", email="u@example.com",
        outcome={"ok": True, "result": {"raw_api_key": ""}},
    )
    assert "register.no_raw_key" in text


def test_submit_runs_inline_when_no_event_loop(monkeypatch) -> None:
    """No asyncio loop → _submit must do the round-trip synchronously."""
    _install_fake_client(monkeypatch, result={"raw_api_key": "az_test_xyz", "username": "u"})

    # Bypass UI-thread side effects: capture-output, save_config, history.
    saved_config = {}
    monkeypatch.setattr("aztea.config.save_config", lambda **kw: saved_config.update(kw))
    monkeypatch.setattr(rm, "_append_to_history", lambda _text: None)
    monkeypatch.setattr(rm, "_push_welcome_if_signed_in", lambda _text: None)
    monkeypatch.setattr(rm, "hide_register_modal", lambda: None)
    monkeypatch.setattr(rm, "_invalidate", lambda: None)

    rm._collected.update(username="u", email="u@example.com", password="pw12345a")

    # No running loop in the test → falls into the inline branch.
    rm._submit()

    assert saved_config.get("api_key") == "az_test_xyz"
    assert rm._submitting[0] is False
