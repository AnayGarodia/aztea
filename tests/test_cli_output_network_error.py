"""Unit tests for ``aztea.cli.output.render_network_error`` — the shared
helper that turns raw urllib3/requests exceptions into actionable inline
copy. Every CLI surface that talks to the network (REPL dispatch,
register modal, login modal, …) routes failures through here so the
user sees the same friendly story regardless of entry point.
"""
from __future__ import annotations

import pytest

from aztea.cli.output import render_network_error


@pytest.fixture
def fake_exc_types():
    """Build exception subclasses mimicking requests.exceptions shapes."""
    class ReadTimeout(Exception):
        pass

    class ConnectTimeout(Exception):
        pass

    class ConnectionError(Exception):
        pass

    return {
        "ReadTimeout": ReadTimeout,
        "ConnectTimeout": ConnectTimeout,
        "ConnectionError": ConnectionError,
    }


def test_render_network_error_handles_read_timeout(capsys, fake_exc_types) -> None:
    exc = fake_exc_types["ReadTimeout"]("Read timed out. (read timeout=30.0)")
    assert render_network_error(exc, code_prefix="repl") is True
    out = capsys.readouterr()
    text = out.out + out.err
    assert "repl.timeout" in text
    assert "took too long" in text.lower()
    # The raw urllib3 message MUST NOT leak through.
    assert "HTTPSConnectionPool" not in text


def test_render_network_error_handles_connect_timeout(capsys, fake_exc_types) -> None:
    exc = fake_exc_types["ConnectTimeout"]("Connection timed out")
    assert render_network_error(exc, code_prefix="agents") is True
    text = capsys.readouterr().err
    assert "agents.timeout" in text


def test_render_network_error_handles_connection_error(capsys, fake_exc_types) -> None:
    exc = fake_exc_types["ConnectionError"]("Failed to establish a new connection")
    assert render_network_error(exc, code_prefix="login") is True
    text = capsys.readouterr().err
    assert "login.network" in text
    assert "couldn't reach" in text.lower()


def test_render_network_error_returns_false_for_unrelated_exception(capsys) -> None:
    """Non-network exceptions must NOT be swallowed — caller does fallback."""
    assert render_network_error(ValueError("bad input"), code_prefix="x") is False
    # Nothing should have been printed.
    text = capsys.readouterr().out + capsys.readouterr().err
    assert text == ""


def test_render_network_error_namespaces_code_by_caller(capsys, fake_exc_types) -> None:
    """code_prefix lets each surface keep its own taxonomy."""
    exc = fake_exc_types["ReadTimeout"]("timed out")
    render_network_error(exc, code_prefix="register")
    assert "register.timeout" in capsys.readouterr().err


def test_render_network_error_detects_by_message_when_class_unknown(capsys) -> None:
    """An obscure subclass with the canonical message still gets caught."""
    class WeirdError(Exception):
        pass

    exc = WeirdError("Read timed out (read timeout=30.0)")
    assert render_network_error(exc, code_prefix="x") is True
    assert "x.timeout" in capsys.readouterr().err
