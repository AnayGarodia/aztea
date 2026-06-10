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
import json
import logging
import struct
import time
from typing import Any

from core import url_security
from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

# Sync gateway budget is 8 s. Page nav + settle alone can take 4–5 s on
# slow targets, so capping additional wait at 6 s used to land us in 504s.
# 6 s keeps worst-case under the budget; callers needing longer waits must
# use the async path (POST /jobs or manage_workflow(action='hire_async')).
_MAX_WAIT_MS = 6_000
_DEFAULT_WAIT_MS = 1_500
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IHDR_OFFSET = 16
_PNG_IHDR_END = 24
_SCRIPT_RESULT_MAX_CHARS = 8_000
_HTML_TRUNCATE = 200_000
_TEXT_TRUNCATE = 60_000
# `screenshot_only` was added 2026-05-20 so callers who want JUST a PNG
# don't get back a 528-byte HTML payload they have to ignore. Both
# `screenshot` and `screenshot_only` render a viewport-sized PNG; the
# latter strips html / visible_text / links / script_result from the
# response (keeping artifact + title + final_url). Callers that asked
# for `screenshot` still get the old shape verbatim.
_VALID_ACTIONS = ("scrape", "screenshot", "screenshot_only", "pdf")
_NAV_TIMEOUT_MS = 15_000
_NETWORK_LOG_LIMIT = 200
_CONSOLE_LOG_LIMIT = 20
_INNER_TEXT_TIMEOUT_MS = 5_000
# Playwright's request.resource_type vocabulary — used to filter the
# network log so a caller after XHR traffic isn't drowned in 180 asset
# rows before the 200-entry cap truncates the data they wanted.
_NETWORK_CAPTURE_TYPES = frozenset({
    "document", "stylesheet", "image", "media", "font", "script",
    "texttrack", "xhr", "fetch", "eventsource", "websocket", "manifest",
    "other",
})

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
    network_types = _normalize_network_types(payload.get("network_capture_types"))
    if isinstance(network_types, dict):
        return network_types  # error envelope
    script = str(payload.get("script") or "").strip()
    viewport = _normalize_viewport(payload.get("viewport"))
    return (url, action, wait_for, wait_ms, capture_network, network_types, script, viewport)


def _normalize_network_types(raw: Any) -> frozenset[str] | dict:
    """Pure: validate the network-log resource-type filter.

    Empty/absent means "capture everything" (the pre-filter behavior).
    Unknown type names are rejected rather than ignored — a typo like
    "ajax" would otherwise silently capture nothing of what the caller
    wanted.
    """
    if not raw:
        return frozenset()
    if not isinstance(raw, list):
        return _err(
            "browser_agent.invalid_network_types",
            "network_capture_types must be a list of resource type strings.",
        )
    cleaned = {str(t).strip().lower() for t in raw if str(t).strip()}
    unknown = cleaned - _NETWORK_CAPTURE_TYPES
    if unknown:
        return _err(
            "browser_agent.invalid_network_types",
            f"Unknown resource types: {sorted(unknown)}. "
            f"Supported: {sorted(_NETWORK_CAPTURE_TYPES)}",
        )
    return frozenset(cleaned)


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
    network_types: frozenset[str],
) -> None:
    """Side-effect: register the response/console listeners that populate the audit log.

    ``network_types`` filters by Playwright resource type (empty = all);
    each row now carries ``resource_type`` so unfiltered captures are
    still classifiable after the fact.
    """
    if capture_network:
        def _on_response(response: Any) -> None:
            resource_type = str(response.request.resource_type or "other")
            if network_types and resource_type not in network_types:
                return
            network_log.append({
                "url": response.url,
                "method": response.request.method,
                "status": response.status,
                "resource_type": resource_type,
            })
        page.on("response", _on_response)

    def _on_console(message: Any) -> None:
        if len(console_messages) < _CONSOLE_LOG_LIMIT:
            console_messages.append(f"{message.type}: {message.text}")

    page.on("console", _on_console)


def _serialize_script_result(raw: Any) -> Any:
    """Pure: best-effort JSON-roundtrip of a Playwright eval return.

    Why: playwright's ``page.evaluate`` already returns plain Python (str /
    int / float / bool / dict / list / None) for JSON-shaped values, but
    can return ``handle`` references for DOM nodes that aren't JSON-safe.
    We round-trip through ``json.dumps(default=str)`` to flatten anything
    weird to a string, then truncate so a huge JSON payload can't blow the
    8s sync budget by serializing for seconds.
    """
    if raw is None:
        return None
    try:
        encoded = json.dumps(raw, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — final fallback path
        return repr(raw)[:_SCRIPT_RESULT_MAX_CHARS]
    if len(encoded) > _SCRIPT_RESULT_MAX_CHARS:
        return encoded[:_SCRIPT_RESULT_MAX_CHARS] + "…[truncated]"
    try:
        return json.loads(encoded)
    except Exception:  # noqa: BLE001
        return encoded


def _wait_after_load(
    page: Any, wait_for: str, wait_ms: int, script: str,
) -> Any:
    """Side-effect: apply the caller's wait/script policy after navigation.

    Returns the script's return value (None when no script ran or it
    returned undefined). Previously the script's return was dropped on the
    floor and callers had to scrape the result out of visible_text.
    """
    if wait_for == "networkidle":
        page.wait_for_load_state("networkidle", timeout=_NAV_TIMEOUT_MS)
    else:
        page.wait_for_selector(wait_for, timeout=_NAV_TIMEOUT_MS)
    if wait_ms > 0:
        page.wait_for_timeout(wait_ms)
    if script:
        return _serialize_script_result(page.evaluate(script))
    return None


def _capture_page(page: Any, action: str) -> dict[str, Any]:
    """Side-effect: read title/html/text/links/screenshot/pdf off the live page."""
    title = page.title() or ""
    raw_html = page.content()
    html = _truncate_html(raw_html)
    try:
        visible_text = page.locator("body").inner_text(timeout=_INNER_TEXT_TIMEOUT_MS)
    except Exception:
        _LOG.debug("body.inner_text failed", exc_info=True)
        visible_text = ""
    raw_text_len = len(visible_text)
    visible_text = _truncate_text(visible_text)
    links = _extract_links(page)
    # `screenshot` is the viewport-only fast path; everything else takes
    # the full-page capture (PDF needs it, scrape just gets it for free).
    # `screenshot_only` matches `screenshot` for the actual image.
    viewport_only = action in ("screenshot", "screenshot_only")
    screenshot_bytes = page.screenshot(full_page=(not viewport_only), type="png")
    pdf_bytes = page.pdf(print_background=True) if action == "pdf" else None
    return {
        "title": title,
        "html": html,
        "html_truncated": len(raw_html) > _HTML_TRUNCATE,
        "visible_text": visible_text,
        "visible_text_truncated": raw_text_len > _TEXT_TRUNCATE,
        "links": links,
        "screenshot_bytes": screenshot_bytes,
        "pdf_bytes": pdf_bytes,
        "final_url": page.url,
    }


def _navigate_and_capture(
    page: Any, url: str, *, action: str, wait_for: str, wait_ms: int, script: str,
) -> tuple[Any, dict[str, Any], Any]:
    """Side-effect: navigate to ``url`` and capture page artifacts.

    Returns ``(response, capture, script_result)``.
    """
    response = page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
    script_result = _wait_after_load(page, wait_for, wait_ms, script)
    return response, _capture_page(page, action), script_result


def _png_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Pure: read (width, height) from a PNG byte string; ``(None, None)`` if malformed.

    Why: previously the screenshot artifact only shipped raw base64 — every
    caller had to decode the PNG header themselves to know the image size.
    The IHDR chunk is always at offset 16 and is big-endian uint32 pair.
    """
    if len(data) < _PNG_IHDR_END or not data.startswith(_PNG_SIGNATURE):
        return None, None
    try:
        width, height = struct.unpack(">II", data[_PNG_IHDR_OFFSET:_PNG_IHDR_END])
        return int(width), int(height)
    except struct.error:
        return None, None


def _bytes_to_artifact(name: str, mime: str, raw: bytes) -> dict[str, Any]:
    """Pure: bytes → artifact dict with base64 data URI.

    For PNG inputs, width/height are read from the IHDR chunk so callers
    don't have to decode the header to know the image size.
    """
    encoded = base64.b64encode(raw).decode("ascii")
    artifact: dict[str, Any] = {
        "name": name,
        "mime": mime,
        "url_or_base64": f"data:{mime};base64,{encoded}",
        "size_bytes": len(raw),
    }
    if mime == "image/png":
        width, height = _png_dimensions(raw)
        artifact["width"] = width
        artifact["height"] = height
    return artifact


def _build_result(
    *, url: str, requested_url: str, action: str, wait_for: str,
    response: Any, capture: dict[str, Any], elapsed_ms: int,
    console_messages: list[str], network_log: list[dict[str, Any]],
    capture_network: bool, script_result: Any,
) -> dict[str, Any]:
    """Pure: shape the agent's response from captured browser data."""
    # `screenshot_only` strips the HTML / visible_text / links / script_result
    # noise from the response. Callers who want both image and HTML continue
    # to use action="screenshot".
    if action == "screenshot_only":
        return {
            "url": url,
            "requested_url": requested_url,
            "title": capture["title"],
            "action": action,
            "wait_for": wait_for,
            "status_code": int(response.status) if response is not None else None,
            "screenshot_artifact": _bytes_to_artifact(
                "screenshot.png", "image/png", capture["screenshot_bytes"],
            ),
            "execution_time_ms": elapsed_ms,
        }
    result: dict[str, Any] = {
        "url": url,
        "requested_url": requested_url,
        "title": capture["title"],
        "html": capture["html"],
        "html_chars": len(capture["html"]),
        "html_truncated": capture["html_truncated"],
        "visible_text": capture["visible_text"],
        "visible_text_truncated": capture["visible_text_truncated"],
        "links": capture["links"],
        "action": action,
        "wait_for": wait_for,
        "status_code": int(response.status) if response is not None else None,
        "screenshot_artifact": _bytes_to_artifact(
            "screenshot.png", "image/png", capture["screenshot_bytes"],
        ),
        "script_result": script_result,
        "execution_time_ms": elapsed_ms,
        "console_messages": console_messages,
    }
    if capture_network:
        result["network_log"] = network_log[:_NETWORK_LOG_LIMIT]
        result["network_log_truncated"] = len(network_log) > _NETWORK_LOG_LIMIT
    if capture["pdf_bytes"] is not None:
        result["pdf_artifact"] = _bytes_to_artifact(
            "page.pdf", "application/pdf", capture["pdf_bytes"],
        )
    return result


def _drive_chromium(
    pw: Any, url: str, *, action: str, wait_for: str, wait_ms: int,
    script: str, capture_network: bool, network_types: frozenset[str],
    viewport: tuple[int, int],
    network_log: list[dict[str, Any]], console_messages: list[str],
) -> dict | tuple[Any, dict[str, Any], Any]:
    """Side-effect: launch Chromium, navigate, and return ``(response, capture, script_result)`` or error envelope."""
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
    _attach_listeners(
        page, network_log, console_messages,
        capture_network=capture_network, network_types=network_types,
    )
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
    url, action, wait_for, wait_ms, capture_network, network_types, script, viewport = parsed
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
                script=script, capture_network=capture_network,
                network_types=network_types, viewport=viewport,
                network_log=network_log, console_messages=console_messages,
            )
    except Exception as exc:
        return _err(
            "browser_agent.navigation_failed",
            f"Browser navigation failed: {type(exc).__name__}: {exc}",
        )
    if isinstance(outcome, dict):  # error envelope from _drive_chromium
        return outcome
    response, capture, script_result = outcome
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    return _build_result(
        url=capture["final_url"], requested_url=url, action=action, wait_for=wait_for,
        response=response, capture=capture, elapsed_ms=elapsed_ms,
        console_messages=console_messages, network_log=network_log,
        capture_network=capture_network, script_result=script_result,
    )
