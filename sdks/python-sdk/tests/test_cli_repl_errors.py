"""Error-path regression tests for the Aztea REPL.

These tests target the *highest-risk surfaces* the V8 plan flagged as
under-covered: network failures inside `/ask`, malformed responses,
and file-I/O failures touching the saved config or anthropic key.

Each test exists to catch a specific class of bug — not to climb
coverage. Network is always mocked (no real ``api.anthropic.com``
calls); filesystem failures are simulated by monkeypatching the path
operations.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# ── /ask network + parsing ────────────────────────────────────────────────


def test_ask_handles_network_timeout(monkeypatch, capsys):
    """A requests.Timeout from _call_anthropic must surface a friendly
    error message, not a Python traceback. The current code path returns
    ``(-1, "Network error: <exc>")`` and ``ask()`` routes that to error()
    with code ``ask.network``."""
    from aztea.cli.repl import ask as _ask

    monkeypatch.setattr(_ask, "_get_api_key", lambda: "az_test")
    import requests as _requests
    def _timeout(*a, **k):
        raise _requests.Timeout("connect timed out")
    monkeypatch.setattr(_ask.requests, "post", _timeout)

    _ask.ask("hello")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Network" in combined or "timeout" in combined.lower()
    # Make sure no traceback leaked.
    assert "Traceback" not in combined


def test_ask_handles_401_invalid_key(monkeypatch, capsys):
    """Anthropic returns 401 for an invalid key. The body is JSON with
    ``{"error": {"message": "..."}}``. We want the user to see *that*
    message as the hint, not raw response text."""
    from aztea.cli.repl import ask as _ask

    monkeypatch.setattr(_ask, "_get_api_key", lambda: "az_broken")

    class _Resp:
        status_code = 401
        def json(self):
            return {"error": {"message": "invalid x-api-key"}}
        @property
        def text(self):  # fallback path
            return '{"error":{"message":"invalid x-api-key"}}'

    monkeypatch.setattr(_ask.requests, "post", lambda *a, **k: _Resp())
    _ask.ask("hello")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "401" in combined
    assert "invalid x-api-key" in combined


def test_ask_handles_429_rate_limit(monkeypatch, capsys):
    """429 should surface as an API error without retrying in a loop."""
    from aztea.cli.repl import ask as _ask
    monkeypatch.setattr(_ask, "_get_api_key", lambda: "az_x")

    calls = {"n": 0}

    class _Resp:
        status_code = 429
        def json(self):
            return {"error": {"message": "rate_limit_exceeded"}}
        @property
        def text(self):
            return "429"

    def _post(*a, **k):
        calls["n"] += 1
        return _Resp()
    monkeypatch.setattr(_ask.requests, "post", _post)

    _ask.ask("hello")
    # No retry storm — exactly one HTTP call.
    assert calls["n"] == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "429" in combined


def test_ask_handles_500_server_error(monkeypatch, capsys):
    """5xx must surface as an API error and never leak the API key."""
    from aztea.cli.repl import ask as _ask
    monkeypatch.setattr(_ask, "_get_api_key", lambda: "az_secret_key_value")

    class _Resp:
        status_code = 500
        def json(self):
            return {"error": {"message": "upstream timeout"}}
        @property
        def text(self):
            return "internal error"

    monkeypatch.setattr(_ask.requests, "post", lambda *a, **k: _Resp())
    _ask.ask("hello")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "500" in combined
    # The key must not appear in the error output — even partially.
    assert "az_secret_key_value" not in combined


def test_ask_handles_malformed_json(monkeypatch, capsys):
    """If the API returns non-JSON, _call_anthropic returns the raw text
    truncated. ask() must surface it as an api_error, not crash."""
    from aztea.cli.repl import ask as _ask
    monkeypatch.setattr(_ask, "_get_api_key", lambda: "az_x")

    class _Resp:
        status_code = 200
        def json(self):
            raise ValueError("not json")
        @property
        def text(self):
            return "<html>HTML response</html>"

    monkeypatch.setattr(_ask.requests, "post", lambda *a, **k: _Resp())
    _ask.ask("hello")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback" not in combined
    # Either "Unexpected response shape" or a parse-related signal.
    assert any(s in combined for s in ("Unexpected", "parse", "200"))


def test_ask_handles_empty_content_array(monkeypatch, capsys):
    """200 OK with ``{"content": []}`` is technically a valid response,
    but produces no text. We surface ``(empty response)`` info — not a
    crash and not a silent hang."""
    from aztea.cli.repl import ask as _ask
    monkeypatch.setattr(_ask, "_get_api_key", lambda: "az_x")

    class _Resp:
        status_code = 200
        def json(self):
            return {"content": []}
        @property
        def text(self):
            return ""

    monkeypatch.setattr(_ask.requests, "post", lambda *a, **k: _Resp())
    _ask.ask("hello")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "empty response" in combined.lower()


def test_ask_env_var_takes_precedence_over_keyfile(monkeypatch, tmp_path):
    """ANTHROPIC_API_KEY env var must win when both env + file are set.
    Otherwise a stale file overrides the user's deliberate env override
    — exactly the surprise behaviour we want to prevent."""
    from aztea.cli.repl import ask as _ask
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env_key_wins")
    # Point config_path to a tmp dir and write a stale file.
    from aztea import config as _config
    monkeypatch.setattr(_config, "config_path", lambda: tmp_path / "config.json")
    (tmp_path / "anthropic.key").write_text("file_key_loses\n")
    assert _ask._get_api_key() == "env_key_wins"


# ── File I/O failure surfaces ─────────────────────────────────────────────


def test_anthropic_key_file_unreadable_returns_none(monkeypatch, tmp_path):
    """OSError while reading ~/.aztea/anthropic.key must NOT crash
    _get_api_key — it returns None and the caller surfaces a "no key"
    error to the user."""
    from aztea.cli.repl import ask as _ask
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from aztea import config as _config
    monkeypatch.setattr(_config, "config_path", lambda: tmp_path / "config.json")
    key_file = tmp_path / "anthropic.key"
    key_file.write_text("doesnt matter")

    # Patch Path.read_text on this specific path to raise.
    real_read = type(key_file).read_text
    def _boom(self, *a, **k):
        if self == key_file:
            raise PermissionError("not readable")
        return real_read(self, *a, **k)
    monkeypatch.setattr(type(key_file), "read_text", _boom)

    assert _ask._get_api_key() is None


def test_ask_no_key_anywhere_surfaces_actionable_error(monkeypatch, tmp_path, capsys):
    """When neither env var nor file is present, /ask must point the
    user at BOTH setup paths in the hint. Previous iterations only
    mentioned the env var, leaving file users stuck."""
    from aztea.cli.repl import ask as _ask
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from aztea import config as _config
    monkeypatch.setattr(_config, "config_path", lambda: tmp_path / "config.json")

    _ask.ask("hello?")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "ANTHROPIC_API_KEY" in combined
    assert "anthropic.key" in combined


def test_claude_md_write_handles_oserror(monkeypatch, tmp_path, capsys):
    """init._append_claude_md_snippet must propagate the OSError so
    init.init() can present it via handle_error. Silent failure here
    means the user thinks setup worked when it didn't."""
    from aztea.cli import init as _init

    # Force the snippet target to a tmp file we'll make unwritable
    # by patching the write call.
    target = tmp_path / "CLAUDE.md"
    target.write_text("existing\n")
    monkeypatch.setattr(_init, "_claude_md_path", lambda: target)

    real_write = type(target).write_text
    def _denied(self, *a, **k):
        if self == target:
            raise PermissionError("read-only fs")
        return real_write(self, *a, **k)
    monkeypatch.setattr(type(target), "write_text", _denied)

    # _append_claude_md_snippet returns a status string or raises.
    with pytest.raises(PermissionError):
        _init._append_claude_md_snippet(target)
