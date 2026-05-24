"""Unit tests for ``aztea.cli.mcp.client_label`` — names the user's client
in install / doctor / init success messages instead of generic "editor".
"""
from __future__ import annotations

from aztea.cli.mcp import client_label


def test_client_label_claude_returns_claude_code() -> None:
    assert client_label("claude") == "Claude Code"


def test_client_label_cursor_returns_cursor() -> None:
    assert client_label("cursor") == "Cursor"


def test_client_label_is_case_insensitive() -> None:
    assert client_label("CLAUDE") == "Claude Code"
    assert client_label("  Cursor  ") == "Cursor"


def test_client_label_unknown_falls_back_to_your_editor() -> None:
    assert client_label("vim") == "your editor"
    assert client_label("") == "your editor"
    assert client_label(None) == "your editor"  # type: ignore[arg-type]
