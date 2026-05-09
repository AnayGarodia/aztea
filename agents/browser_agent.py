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
import logging
import time
from typing import Any

from core import url_security
from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

_MAX_WAIT_MS = 10_000
_DEFAULT_WAIT_MS = 1_500
_HTML_TRUNCATE = 200_000
_TEXT_TRUNCATE = 60_000
_VALID_ACTIONS = ("scrape", "screenshot", "pdf")
_NAV_TIMEOUT_MS = 15_000
_NETWORK_LOG_LIMIT = 200
_CONSOLE_LOG_LIMIT = 20
_INNER_TEXT_TIMEOUT_MS = 5_000

_VIEWPORT_WIDTH_DEFAULT = 1280
_VIEWPORT_HEIGHT_DEFAULT = 720
_VIEWPORT_WIDTH_MIN = 320
_VIEWPORT_HEIGHT_MIN = 240
_VIEWPORT_WIDTH_MAX = 3840
_VIEWPORT_HEIGHT_MAX = 2160

_USER_AGENT = "Aztea-Browser-Agent/1.0 (headless; for authorized auditing)"



def _normalize_action(value: Any) -> str:
    """Pure: validate the ``action`` payload key against ``_VALID_ACTIONS``."""
    action = str(value or "scrape").strip().lower()
    if action not in _VALID_ACTIONS:
        raise ValueError(f"action must be one of: {', '.join(sorted(_VALID_ACTIONS))}")
    return action


def _normalize_wait_for(value: Any) -> str:
    """Pure: ``wait_for`` is either a CSS selector or 'networkidle'; default 'networkidle'."""
    wait_for = str(value or "networkidle").strip()
    return wait_for or "networkidle"


def _extract_links(page: Any) -> list[dict[str, str]]:  # noqa: ANN401
    """Side-effect: read up to 50 anchor tags from the live page; ``[]`` on JS error."""
    try:
        raw_links = page.eval_on_selector_all(
            "a[href]",
            """
            nodes => nodes.slice(0, 50).map(node => ({
              text: (node.textContent || '').trim().slice(0, 120),
              href: node.href || ''
            }))
            """,
        )
    except Exception:
        _LOG.debug("link extraction failed", exc_info=True)
        return []
    links: list[dict[str, str]] = []
    for item in raw_links or []:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href") or "").strip()
        text = str(item.get("text") or "").strip()
        if href:
            links.append({"text": text, "href": href})
    return links


def _install_request_guard(context: Any) -> None:  # noqa: ANN401
    """Abort browser requests that pivot to blocked/private targets."""

    def _guard(route: Any) -> None:  # noqa: ANN401
        request = route.request
        try:
            url_security.validate_outbound_url(request.url, "url")
        except Exception:
            route.abort()
            return
        route.continue_()

    context.route("**/*", _guard)


def _normalize_viewport(vp: Any) -> tuple[int, int]:
    """Pure: clamp viewport dims to the allowed range; ignore non-dict input."""
    if not isinstance(vp, dict):
        vp = {}
    try:
        width = max(_VIEWPORT_WIDTH_MIN, min(int(vp.get("width") or _VIEWPORT_WIDTH_DEFAULT), _VIEWPORT_WIDTH_MAX))
        height = max(_VIEWPORT_HEIGHT_MIN, min(int(vp.get("height") or _VIEWPORT_HEIGHT_DEFAULT), _VIEWPORT_HEIGHT_MAX))
        return width, height
    except (TypeError, ValueError):
        return _VIEWPORT_WIDTH_DEFAULT, _VIEWPORT_HEIGHT_DEFAULT


def _truncate_html(html: str) -> str:
    """Pure: HTML-comment truncation marker so the result remains valid HTML."""
    if len(html) <= _HTML_TRUNCATE:
        return html
    return html[:_HTML_TRUNCATE] + f"\n<!-- [truncated {len(html) - _HTML_TRUNCATE} chars] -->"


def _truncate_text(text: str) -> str:
    """Pure: plain-text truncation marker."""
    if len(text) <= _TEXT_TRUNCATE:
        return text
    return text[:_TEXT_TRUNCATE] + f"\n[truncated {len(text) - _TEXT_TRUNCATE} chars]"


def _normalize_run_inputs(payload: dict[str, Any]) -> dict | tuple[Any, ...]:
    """Pure: validate ``payload``; returns the parsed bag or an error envelope."""
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("browser_agent.missing_url", "url is required.")
    try:
        url = url_security.validate_outbound_url(raw_url, "url")
    except Exception as exc:
        return _err("browser_agent.url_blocked", str(exc))
    try:
        action = _normalize_action(payload.get("action"))
    except ValueError as exc:
        return _err("browser_agent.invalid_action", str(exc))
    wait_for = _normalize_wait_for(payload.get("wait_for"))
    try:
        wait_ms = min(int(payload.get("wait_ms") or _DEFAULT_WAIT_MS), _MAX_WAIT_MS)
    except (TypeError, ValueError):
        wait_ms = _DEFAULT_WAIT_MS
    capture_network = bool(payload.get("capture_network", False))
    script = str(payload.get("script") or "").strip()
    viewport = _normalize_viewport(payload.get("viewport"))
    return (url, action, wait_for, wait_ms, capture_network, script, viewport)


def _import_playwright() -> Any:
    """Side-effect: lazy Playwright import; returns the module or an error envelope.

    Why (rule 11): Playwright is heavy and not provisioned on every worker.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
        return sync_playwright
    except ImportError:
        return _err(
            "browser_agent.tool_unavailable",
            "playwright is not installed on this executor. Install it with: "
            "pip install playwright && playwright install chromium",
        )


def _is_chromium_missing(exc: BaseException) -> bool:
    """Pure: detect the 'chromium not installed' signal from a launch failure."""
    msg = str(exc)
    return "Executable doesn't exist" in msg or "playwright install" in msg


def _attach_listeners(
    page: Any, network_log: list[dict[str, Any]],
    console_messages: list[str], *, capture_network: bool,
) -> None:
    """Side-effect: register the response/console listeners that populate the audit log."""
    if capture_network:
        def _on_response(response: Any) -> None:
            network_log.append({
                "url": response.url,
                "method": response.request.method,
                "status": response.status,
            })
        page.on("response", _on_response)

    def _on_console(message: Any) -> None:
        if len(console_messages) < _CONSOLE_LOG_LIMIT:
            console_messages.append(f"{message.type}: {message.text}")

    page.on("console", _on_console)


def _wait_after_load(page: Any, wait_for: str, wait_ms: int, script: str) -> None:
    """Side-effect: apply the caller's wait/script policy after navigation."""
    if wait_for == "networkidle":
        page.wait_for_load_state("networkidle", timeout=_NAV_TIMEOUT_MS)
    else:
        page.wait_for_selector(wait_for, timeout=_NAV_TIMEOUT_MS)
    if wait_ms > 0:
        page.wait_for_timeout(wait_ms)
    if script:
        page.evaluate(script)


def _capture_page(page: Any, action: str) -> dict[str, Any]:
    """Side-effect: read title/html/text/links/screenshot/pdf off the live page."""
    title = page.title() or ""
    html = _truncate_html(page.content())
    try:
        visible_text = page.locator("body").inner_text(timeout=_INNER_TEXT_TIMEOUT_MS)
    except Exception:
        _LOG.debug("body.inner_text failed", exc_info=True)
        visible_text = ""
    visible_text = _truncate_text(visible_text)
    links = _extract_links(page)
    screenshot_bytes = page.screenshot(full_page=(action != "screenshot"), type="png")
    pdf_bytes = page.pdf(print_background=True) if action == "pdf" else None
    return {
        "title": title,
        "html": html,
        "visible_text": visible_text,
        "links": links,
        "screenshot_bytes": screenshot_bytes,
        "pdf_bytes": pdf_bytes,
        "final_url": page.url,
    }


def _navigate_and_capture(
    page: Any, url: str, *, action: str, wait_for: str, wait_ms: int, script: str,
) -> tuple[Any, dict[str, Any]]:
    """Side-effect: navigate to ``url`` and capture page artifacts; returns (response, capture)."""
    response = page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
    _wait_after_load(page, wait_for, wait_ms, script)
    return response, _capture_page(page, action)


def _bytes_to_artifact(name: str, mime: str, raw: bytes) -> dict[str, Any]:
    """Pure: bytes → artifact dict with base64 data URI."""
    encoded = base64.b64encode(raw).decode("ascii")
    return {
        "name": name,
        "mime": mime,
        "url_or_base64": f"data:{mime};base64,{encoded}",
        "size_bytes": len(raw),
    }


def _build_result(
    *, url: str, requested_url: str, action: str, wait_for: str,
    response: Any, capture: dict[str, Any], elapsed_ms: int,
    console_messages: list[str], network_log: list[dict[str, Any]],
    capture_network: bool,
) -> dict[str, Any]:
    """Pure: shape the agent's response from captured browser data."""
    result: dict[str, Any] = {
        "url": url,
        "requested_url": requested_url,
        "title": capture["title"],
        "html": capture["html"],
        "html_chars": len(capture["html"]),
        "visible_text": capture["visible_text"],
        "links": capture["links"],
        "action": action,
        "wait_for": wait_for,
        "status_code": int(response.status) if response is not None else None,
        "screenshot_artifact": _bytes_to_artifact(
            "screenshot.png", "image/png", capture["screenshot_bytes"],
        ),
        "execution_time_ms": elapsed_ms,
        "console_messages": console_messages,
    }
    if capture_network:
        result["network_log"] = network_log[:_NETWORK_LOG_LIMIT]
    if capture["pdf_bytes"] is not None:
        result["pdf_artifact"] = _bytes_to_artifact(
            "page.pdf", "application/pdf", capture["pdf_bytes"],
        )
    return result


def _drive_chromium(
    pw: Any, url: str, *, action: str, wait_for: str, wait_ms: int,
    script: str, capture_network: bool, viewport: tuple[int, int],
    network_log: list[dict[str, Any]], console_messages: list[str],
) -> dict | tuple[Any, dict[str, Any]]:
    """Side-effect: launch Chromium, navigate, and return ``(response, capture)`` or error envelope."""
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as launch_exc:
        if _is_chromium_missing(launch_exc):
            return _err(
                "tool_unavailable",
                "Headless Chromium is not provisioned on this worker. "
                "Run `playwright install chromium` on the executor. "
                "The call was not billed.",
            )
        raise
    width, height = viewport
    context = browser.new_context(
        viewport={"width": width, "height": height}, user_agent=_USER_AGENT,
    )
    _install_request_guard(context)
    page = context.new_page()
    _attach_listeners(page, network_log, console_messages, capture_network=capture_network)
    try:
        return _navigate_and_capture(
            page, url, action=action, wait_for=wait_for, wait_ms=wait_ms, script=script,
        )
    finally:
        context.close()
        browser.close()


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Navigate a URL with headless Chromium and return page content + screenshot.

    Why: the agent shells out to Playwright because hosted JS evaluation is the
    only way to capture post-render DOM, console output, and live network log.
    """
    parsed = _normalize_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed  # error envelope
    url, action, wait_for, wait_ms, capture_network, script, viewport = parsed
    sync_playwright = _import_playwright()
    if isinstance(sync_playwright, dict):
        return sync_playwright  # error envelope

    t_start = time.monotonic()
    network_log: list[dict[str, Any]] = []
    console_messages: list[str] = []
    try:
        with sync_playwright() as pw:
            outcome = _drive_chromium(
                pw, url, action=action, wait_for=wait_for, wait_ms=wait_ms,
                script=script, capture_network=capture_network, viewport=viewport,
                network_log=network_log, console_messages=console_messages,
            )
    except Exception as exc:
        return _err(
            "browser_agent.navigation_failed",
            f"Browser navigation failed: {type(exc).__name__}: {exc}",
        )
    if isinstance(outcome, dict):  # error envelope from _drive_chromium
        return outcome
    response, capture = outcome
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    return _build_result(
        url=capture["final_url"], requested_url=url, action=action, wait_for=wait_for,
        response=response, capture=capture, elapsed_ms=elapsed_ms,
        console_messages=console_messages, network_log=network_log,
        capture_network=capture_network,
    )
