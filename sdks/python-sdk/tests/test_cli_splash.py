"""Smoke tests for the `aztea` no-arg splash.

These guard the visual contract: the wordmark prints, the tagline prints,
every quickstart shortcut renders, and signed-in vs signed-out modes both
work without exploding when stdout is captured (no TTY).
"""
from __future__ import annotations

from io import StringIO

import pytest

from aztea.cli import splash


def _capture(monkeypatch, meta):
    monkeypatch.setattr(splash, "_signed_in_meta", lambda: meta)
    # Force the plain renderer path so no Rich-only ANSI escapes muddy assertions.
    monkeypatch.setattr(splash, "_HAS_RICH", False)
    buf = StringIO()

    class _Sink:
        def print(self, *a, **k):
            buf.write(" ".join(str(x) for x in a) + "\n")

    monkeypatch.setattr(splash, "console", _Sink())
    splash.render_splash()
    return buf.getvalue()


def test_splash_signed_out_shows_login_cta(monkeypatch):
    out = _capture(monkeypatch, None)
    assert "the clearing house for agent commerce" in out
    assert "aztea login" in out
    assert "signed out" in out
    # Tagline pillars all surface
    for pillar in ("discovery", "escrow", "signed receipts", "recourse"):
        assert pillar in out


def test_splash_signed_in_shows_user_and_shortcuts(monkeypatch):
    meta = {"username": "yctest", "base_url": "https://aztea.ai"}
    out = _capture(monkeypatch, meta)
    assert "yctest" in out
    assert "https://aztea.ai" in out
    for cmd, _desc in splash._SHORTCUTS_AUTHED:
        assert cmd in out


def test_logo_rows_are_width_stable():
    # All gradient rows must be the same width as the canonical glyph width.
    assert len(splash._LOGO_ROWS) == len(splash._LOGO_GRADIENT)
    for row in splash._LOGO_ROWS:
        assert len(row) == splash._LOGO_WIDTH


@pytest.mark.parametrize("meta", [None, {"username": "u", "base_url": "https://x"}])
def test_render_does_not_raise(monkeypatch, meta):
    _capture(monkeypatch, meta)
