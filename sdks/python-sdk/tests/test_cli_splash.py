"""Smoke tests for the V3 Aztea banner.

The V3 banner = V1 visual layout (gradient wordmark + rounded quickstart
panel) MINUS the tagline and the four-pillar capability ticker. The
quickstart panel lists slash commands now, not bash commands, because the
default `aztea` invocation opens the REPL.

Tests run against the plain-text renderer (no Rich ANSI escapes) so
substring assertions are stable.
"""
from __future__ import annotations

from io import StringIO

import pytest

from aztea.cli import splash


def _capture(monkeypatch, meta):
    monkeypatch.setattr(splash, "_signed_in_meta", lambda: meta)
    monkeypatch.setattr(splash, "_HAS_RICH", False)
    buf = StringIO()

    class _Sink:
        def print(self, *a, **k):
            buf.write(" ".join(str(x) for x in a) + "\n")

    monkeypatch.setattr(splash, "console", _Sink())
    splash.render_banner()
    return buf.getvalue()


# ── Layout ────────────────────────────────────────────────────────────────


def test_banner_signed_out_lists_slash_commands(monkeypatch):
    out = _capture(monkeypatch, None)
    # Quickstart panel surfaces slash commands.
    assert "/login" in out
    assert "/claude-code" in out
    assert "/help" in out
    # /agents is intentionally NOT in the unauth panel (server rejects
    # /registry/agents without a key — would set a bad first-run
    # expectation).
    assert "/agents" not in out
    # Footer pointer.
    assert "Type /help for commands" in out
    assert "Ctrl-D" in out


def test_banner_signed_in_default_quickstart(monkeypatch):
    """When MCP is registered, the authed Quickstart leads with
    /claude-code and includes /status."""
    meta = {"username": "yctest", "base_url": "https://aztea.ai"}
    monkeypatch.setattr(splash, "_mcp_registered_now", lambda: True)
    out = _capture(monkeypatch, meta)
    # Default authed quickstart entries.
    assert "/claude-code" in out
    assert "/hire" in out
    assert "/agents" in out
    assert "/status" in out
    assert "/ask" in out
    # No /init tip when MCP is already registered.
    assert "wired into Claude Code yet" not in out
    # Status line includes username + host.
    assert "Signed in as yctest" in out
    assert "aztea.ai" in out


def test_banner_signed_in_without_mcp_promotes_init(monkeypatch):
    """When MCP isn't yet registered, /init leads the Quickstart and
    a one-line tip explains why."""
    meta = {"username": "yctest", "base_url": "https://aztea.ai"}
    monkeypatch.setattr(splash, "_mcp_registered_now", lambda: False)
    out = _capture(monkeypatch, meta)
    # /init is in the panel.
    assert "/init" in out
    # The tip is rendered above the panel.
    assert "wired into Claude Code yet" in out
    # /ask still shown — it's value-add regardless of init state.
    assert "/ask" in out
    # /status is bumped out to keep the panel height stable.
    # (Discoverable via /help; we just shouldn't promote it here.)


def test_banner_unauth_has_no_status_line(monkeypatch):
    """Signed-out users have no username, so no status line is rendered.
    The panel itself (leading with /login) tells the story."""
    monkeypatch.setattr(splash, "_mcp_registered_now", lambda: False)
    out = _capture(monkeypatch, None)
    assert "Signed in as" not in out
    assert "wired into Claude Code yet" not in out  # tip is authed-only
    assert "/login" in out
    assert "/ask" in out                            # /ask works unauth too
    # No version line / "not signed in" verbose state.
    assert "v1." not in out


def test_banner_drops_v1_tagline_and_ticker(monkeypatch):
    """V3 must never reintroduce the V1 tagline or the capability ticker."""
    out = _capture(monkeypatch, None)
    assert "hire specialist agents from your terminal" not in out
    for pillar in ("live CVE data", "real code execution", "browser automation"):
        assert pillar not in out


def test_banner_drops_v2_try_rows(monkeypatch):
    """V3 must never reintroduce the V2 `aztea try` rows."""
    out = _capture(monkeypatch, None)
    assert "aztea try cve-lookup" not in out
    assert "free demos, no signup" not in out


# ── Wordmark integrity ────────────────────────────────────────────────────


def test_logo_rows_are_width_stable():
    """The ANSI Shadow wordmark must keep its 41-col width across rows."""
    for row in splash._LOGO_ROWS:
        assert len(row) == splash._LOGO_WIDTH


def test_gradients_have_six_tiers_each():
    """Both palettes must have one style per wordmark row."""
    assert len(splash._GRADIENT_DARK) == len(splash._LOGO_ROWS)
    assert len(splash._GRADIENT_LIGHT) == len(splash._LOGO_ROWS)


# ── Theme adaptation ──────────────────────────────────────────────────────


def test_detect_terminal_mode_honors_env_override(monkeypatch):
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setenv("AZTEA_TERMINAL_THEME", "light")
    assert splash._detect_terminal_mode() == "light"
    monkeypatch.setenv("AZTEA_TERMINAL_THEME", "dark")
    assert splash._detect_terminal_mode() == "dark"


def test_detect_terminal_mode_reads_colorfgbg(monkeypatch):
    monkeypatch.delenv("AZTEA_TERMINAL_THEME", raising=False)
    monkeypatch.setenv("COLORFGBG", "15;0")    # white-on-black → dark
    assert splash._detect_terminal_mode() == "dark"
    monkeypatch.setenv("COLORFGBG", "0;15")    # black-on-white → light
    assert splash._detect_terminal_mode() == "light"
    # Some terminals emit fg;variant;bg — we read the last field.
    monkeypatch.setenv("COLORFGBG", "15;default;0")
    assert splash._detect_terminal_mode() == "dark"


def test_detect_terminal_mode_defaults_to_dark(monkeypatch):
    monkeypatch.delenv("AZTEA_TERMINAL_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert splash._detect_terminal_mode() == "dark"


def test_theme_palette_swaps_with_mode(monkeypatch):
    monkeypatch.setenv("AZTEA_TERMINAL_THEME", "dark")
    pal = splash._theme_palette()
    assert pal["gradient"] == splash._GRADIENT_DARK
    assert pal["accent"] == splash._ACCENT_DARK
    monkeypatch.setenv("AZTEA_TERMINAL_THEME", "light")
    pal = splash._theme_palette()
    assert pal["gradient"] == splash._GRADIENT_LIGHT
    assert pal["accent"] == splash._ACCENT_LIGHT


# ── Render does not raise across modes ────────────────────────────────────


@pytest.mark.parametrize("meta", [None, {"username": "u", "base_url": "https://x"}])
def test_render_does_not_raise(monkeypatch, meta):
    _capture(monkeypatch, meta)


def test_render_splash_back_compat_alias(monkeypatch):
    """The old `render_splash` name is kept as an alias for older callers."""
    assert splash.render_splash is splash.render_banner
