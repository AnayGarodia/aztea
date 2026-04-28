"""
browser_agent.py — Headless Chromium browser via Playwright

Input:
  {
    "url": "https://example.com",          # required, SSRF-checked
    "wait_ms": 2000,                        # optional — extra wait after load
    "capture_network": false,               # include request log (default false)
    "viewport": {"width": 1280, "height": 720}
  }

Output:
  {
    "url": str,
    "title": str,
    "html": str,
    "html_chars": int,
    "screenshot_artifact": {"name": str, "mime": str, "url_or_base64": str, "size_bytes": int},
    "network_log": [{"url": str, "method": str, "status": int}],   # if capture_network=true
    "execution_time_ms": int,
    "error": str   # only on failure
  }
"""
from __future__ import annotations

import base64
import time
from typing import Any

from core import url_security

_MAX_WAIT_MS = 10_000
_DEFAULT_WAIT_MS = 1_500
_HTML_TRUNCATE = 200_000


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def run(payload: dict[str, Any]) -> dict[str, Any]:
    url = str(payload.get("url") or "").strip()
    if not url:
        return _err("browser_agent.missing_url", "url is required.")

    # SSRF guard — all outbound URLs must pass the platform security check
    try:
        url = url_security.validate_outbound_url(url, "url")
    except Exception as exc:
        return _err("browser_agent.url_blocked", str(exc))

    wait_ms = min(int(payload.get("wait_ms") or _DEFAULT_WAIT_MS), _MAX_WAIT_MS)
    capture_network = bool(payload.get("capture_network", False))
    vp = payload.get("viewport") or {}
    vp_width = max(320, min(int(vp.get("width") or 1280), 3840))
    vp_height = max(240, min(int(vp.get("height") or 720), 2160))

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
    except ImportError:
        return _err(
            "browser_agent.tool_unavailable",
            "playwright is not installed on this executor. Install it with: pip install playwright && playwright install chromium",
        )

    t_start = time.monotonic()
    network_log: list[dict[str, Any]] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": vp_width, "height": vp_height},
                user_agent="Aztea-Browser-Agent/1.0 (headless; for authorized auditing)",
            )
            page = context.new_page()

            if capture_network:
                def _on_response(response: Any) -> None:  # noqa: ANN401
                    network_log.append({
                        "url": response.url,
                        "method": response.request.method,
                        "status": response.status,
                    })
                page.on("response", _on_response)

            page.goto(url, wait_until="networkidle", timeout=15_000)
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)

            title = page.title() or ""
            html = page.content()
            if len(html) > _HTML_TRUNCATE:
                html = html[:_HTML_TRUNCATE] + f"\n<!-- [truncated {len(html) - _HTML_TRUNCATE} chars] -->"

            screenshot_bytes = page.screenshot(full_page=False, type="png")
            context.close()
            browser.close()
    except Exception as exc:
        return _err("browser_agent.navigation_failed", f"Browser navigation failed: {type(exc).__name__}: {exc}")

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    b64_screenshot = base64.b64encode(screenshot_bytes).decode("ascii")
    data_uri = f"data:image/png;base64,{b64_screenshot}"

    result: dict[str, Any] = {
        "url": url,
        "title": title,
        "html": html,
        "html_chars": len(html),
        "screenshot_artifact": {
            "name": "screenshot.png",
            "mime": "image/png",
            "url_or_base64": data_uri,
            "size_bytes": len(screenshot_bytes),
        },
        "execution_time_ms": elapsed_ms,
    }
    if capture_network:
        result["network_log"] = network_log[:200]
    return result
