"""core/web/stealth_browser selector.

Default OFF must be byte-equivalent (stock Playwright, honest UA, no launch kwargs).
ON swaps to a realistic UA and degrades to stock Playwright (without raising) when
patchright is not installed.
"""

from __future__ import annotations

from core.web import stealth_browser as sb


def test_off_by_default_is_stock_and_honest_ua(monkeypatch):
    monkeypatch.delenv("AZTEA_STEALTH_BROWSER", raising=False)
    assert sb.launch_kwargs() == {}
    assert sb.context_kwargs(honest_ua="Aztea/1.0") == {"user_agent": "Aztea/1.0"}
    assert sb.playwright_module().__module__ == "playwright.sync_api"


def test_on_swaps_to_a_realistic_ua(monkeypatch):
    monkeypatch.setenv("AZTEA_STEALTH_BROWSER", "1")
    ck = sb.context_kwargs(honest_ua="Aztea/1.0")
    assert ck["user_agent"] != "Aztea/1.0" and "Mozilla" in ck["user_agent"]


def test_on_returns_a_module_even_without_patchright(monkeypatch):
    # patchright is an optional dep; the selector must degrade, not raise.
    monkeypatch.setenv("AZTEA_STEALTH_BROWSER", "1")
    assert callable(sb.playwright_module())


def test_on_honours_channel_env(monkeypatch):
    monkeypatch.setenv("AZTEA_STEALTH_BROWSER", "1")
    monkeypatch.setenv("AZTEA_STEALTH_BROWSER_CHANNEL", "chrome")
    assert sb.launch_kwargs() == {"channel": "chrome"}


def test_ua_is_env_overridable(monkeypatch):
    monkeypatch.setenv("AZTEA_STEALTH_BROWSER", "1")
    monkeypatch.setenv("AZTEA_STEALTH_USER_AGENT", "Custom/9.9")
    assert sb.context_kwargs(honest_ua="Aztea/1.0") == {"user_agent": "Custom/9.9"}
