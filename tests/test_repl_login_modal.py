"""Unit tests for the in-REPL login modal — friendly error mapping.

The full login flow goes through a Typer command path that's tested
elsewhere; here we only assert that client-side network failures
(timeout, connection error) get translated into actionable inline
copy instead of leaking raw urllib3 messages.
"""
from __future__ import annotations

from aztea.cli.repl import login_modal as lm


def test_surface_login_error_maps_read_timeout(capsys) -> None:
    class ReadTimeout(Exception):
        pass

    lm._surface_login_error(ReadTimeout("HTTPSConnectionPool: Read timed out. (read timeout=30.0)"))
    out = capsys.readouterr()
    text = out.out + out.err
    assert "login.timeout" in text
    assert "took too long" in text.lower()
    assert "HTTPSConnectionPool" not in text


def test_surface_login_error_maps_connection_error(capsys) -> None:
    class ConnectionError(Exception):
        pass

    lm._surface_login_error(ConnectionError("Failed to establish a new connection"))
    out = capsys.readouterr()
    text = out.out + out.err
    assert "login.network" in text
    assert "couldn't reach" in text.lower()


def test_surface_login_error_falls_back_to_raw_message_for_unknown(capsys) -> None:
    lm._surface_login_error(RuntimeError("something else broke"))
    out = capsys.readouterr()
    text = out.out + out.err
    # Generic path keeps the original message visible after the prefix.
    assert "Sign-in failed" in text
    assert "something else broke" in text
