"""Gated stealth-browser selector for the web agents (Phase 4, 2026-06-02).

# OWNS: choosing between stock Playwright and patchright (an undetected, drop-in
#        Playwright) for headless browsing, plus the launch/context options that
#        reduce automation fingerprints. Gated by AZTEA_STEALTH_BROWSER.
# NOT OWNS: the SSRF guard (agents install it via core.url_security), the proxy /
#           remote-browser vendor seam (core.web.fetch_backend), or the interaction
#           loop (agents._web_interact / agents.web_actor).
# INVARIANTS:
#   * Default OFF: with AZTEA_STEALTH_BROWSER unset, playwright_module() returns stock
#     Playwright and context_kwargs() keeps the honest Aztea user-agent, so behavior
#     is byte-equivalent to before.
#   * No silent fallback: if stealth is requested but patchright is not installed, we
#     log a clear warning and degrade to stock Playwright — never silently.
# DECISIONS:
#   * patchright is a drop-in for playwright.sync_api, so the selector returns the
#     sync_playwright callable and call sites keep their pw.chromium.launch(...) shape.
#   * A real-Chrome channel gives the strongest stealth but is not guaranteed installed
#     on a server, so it is opt-in via AZTEA_STEALTH_BROWSER_CHANNEL (default: the
#     patched bundled chromium).
#   * Stealth mode drops the bot-identifying "Aztea-Web-Actor (headless)" user-agent
#     and substitutes a realistic desktop-Chrome string — advertising a headless Aztea
#     UA would defeat the evasion outright.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from core import feature_flags

_LOG = logging.getLogger(__name__)

# A realistic, current desktop-Chrome UA used only in stealth mode. UA strings go
# stale, so it is env-overridable; the default is refreshed when patchright bumps.
_DEFAULT_STEALTH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _stealth_user_agent() -> str:
    """The UA presented in stealth mode (env-overridable so it can be refreshed
    without a redeploy when it starts looking dated to fingerprinters)."""
    return os.environ.get("AZTEA_STEALTH_USER_AGENT", "").strip() or _DEFAULT_STEALTH_UA


def playwright_module() -> Any:
    """Return the sync_playwright entrypoint — patchright in stealth mode, else stock.

    Raises ImportError if no Playwright is installed at all (the caller turns that into
    a structured 'tool_unavailable'). When stealth is requested but patchright is
    absent, logs a warning and degrades to stock Playwright: a loud, documented
    fallback rather than a silent one, because an operator who set the flag must know
    the evasion did not actually engage.
    """
    if feature_flags.stealth_browser_enabled():
        try:
            from patchright.sync_api import sync_playwright  # type: ignore[import]
            return sync_playwright
        except ImportError:
            _LOG.warning(
                "AZTEA_STEALTH_BROWSER=1 but patchright is not installed; "
                "degrading to stock Playwright (no stealth). "
                "Install with: pip install patchright && patchright install chromium"
            )
    from playwright.sync_api import sync_playwright  # type: ignore[import]
    return sync_playwright


def launch_kwargs() -> dict[str, Any]:
    """kwargs to splat into chromium.launch(...). Empty by default (callers still pass
    headless=True). In stealth mode, honours AZTEA_STEALTH_BROWSER_CHANNEL (e.g. 'chrome')
    so an operator with real Chrome installed gets the strongest fingerprint; unset
    means the patched bundled chromium, which is still launched headless by the caller.
    """
    if not feature_flags.stealth_browser_enabled():
        return {}
    channel = os.environ.get("AZTEA_STEALTH_BROWSER_CHANNEL", "").strip()
    return {"channel": channel} if channel else {}


def context_kwargs(*, honest_ua: str) -> dict[str, Any]:
    """kwargs to splat into browser.new_context(...).

    Off (default): keep the caller's honest Aztea UA — byte-equivalent to before.
    On: substitute a realistic desktop-Chrome UA, since the honest headless-Aztea
    string would itself flag the request as a bot.
    """
    if not feature_flags.stealth_browser_enabled():
        return {"user_agent": honest_ua}
    return {"user_agent": _stealth_user_agent()}
