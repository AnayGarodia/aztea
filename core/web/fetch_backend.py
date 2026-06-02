"""Pluggable fetch backend (Phase C): direct vs proxy vs remote-browser.

# OWNS: env-driven selection of HOW outbound web fetches are made — directly, or
#        through a paid proxy / anti-bot endpoint (Bright Data / ScrapingBee / Oxylabs).
# NOT OWNS: the SSRF policy (always core.url_security — the TARGET url is validated
#           whether or not a proxy is used) or the fetch loop (agents._site_fetch).
# INVARIANTS:
#   * Default is 'direct'. A proxy is used ONLY when AZTEA_FETCH_PROXY_URL is set, so
#     the off-by-default rollout is the no-proxy path (behavior-equivalent to before).
#   * The target URL is SSRF-validated regardless of backend. With a proxy, the
#     per-connect IP pin is moot (the proxy resolves the target host, not us), so the
#     proxy operator is trusted — a documented tradeoff, the price of anti-bot reach.
# DECISIONS:
#   * v1 supports 'direct' and 'proxy' (httpx proxy — the cheap lever that unlocks a
#     residential pool). A hosted-browser backend (Browserbase-style) is a documented
#     extension point: it needs the vendor's session/CDP API, not just a proxy URL, so
#     remote_browser_config() exposes the config but the Chromium wiring raises a clear
#     'not wired' error rather than silently falling back — an operator who asked for a
#     remote browser must know it isn't on yet.
"""

from __future__ import annotations

import os

BACKEND_DIRECT = "direct"
BACKEND_PROXY = "proxy"
BACKEND_REMOTE_BROWSER = "remote_browser"


def backend_name() -> str:
    """The configured fetch backend (default 'direct')."""
    return os.environ.get("AZTEA_FETCH_BACKEND", "").strip().lower() or BACKEND_DIRECT


def proxy_url() -> str | None:
    """The configured outbound proxy URL, or None.

    Setting this (e.g. to a Bright Data / ScrapingBee endpoint) is what flips the HTTP
    fetch path onto a proxy — the single env var an operator changes to get anti-bot
    reach on Cloudflare-protected sites.
    """
    return os.environ.get("AZTEA_FETCH_PROXY_URL", "").strip() or None


def httpx_kwargs() -> dict:
    """kwargs to splat into httpx.Client so a fetch routes through the configured proxy
    when one is set. Empty (direct) by default — so the off path adds nothing."""
    proxy = proxy_url()
    return {"proxy": proxy} if proxy else {}


def remote_browser_config() -> dict | None:
    """Hosted-browser backend config (Browserbase-style), or None.

    Extension point: returns {provider, api_key} when AZTEA_FETCH_BACKEND=remote_browser.
    The navigator's Chromium path would dial the vendor's CDP endpoint instead of
    launching local Chromium. NOT wired in v1 — call sites that honor it must raise a
    clear 'remote browser not wired' error rather than silently using local Chromium.
    """
    if backend_name() != BACKEND_REMOTE_BROWSER:
        return None
    return {
        "provider": os.environ.get("AZTEA_REMOTE_BROWSER_PROVIDER", "browserbase").strip().lower(),
        "api_key": os.environ.get("AZTEA_REMOTE_BROWSER_API_KEY", "").strip(),
    }
