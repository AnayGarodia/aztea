"""
accessibility_auditor.py — Run axe-core against any public URL via Playwright.

Input:
  {
    "url": "https://example.com",                # required
    "tags": ["wcag2a", "wcag2aa", "wcag21aa"],   # optional, default WCAG-2.1-AA set
    "viewport": {"width": 1280, "height": 800},  # optional
    "wait_ms": 1500                               # optional, post-load delay
  }

Output:
  {
    "url": str,
    "final_url": str,
    "page_title": str,
    "axe_version": str,
    "test_engine": str,
    "violations": [
      {
        "id": str,
        "impact": "minor|moderate|serious|critical",
        "tags": [str],
        "help": str,
        "help_url": str,
        "node_count": int,
        "nodes": [{"target": [str], "html": str, "failure_summary": str}]
      }
    ],
    "totals": {
      "violations": int,
      "critical": int,
      "serious": int,
      "moderate": int,
      "minor": int,
      "passes": int,
      "incomplete": int
    },
    "billing_units_actual": int   # always 1
  }

Runtime:
  Playwright + Chromium (already provisioned by browser_agent / visual_regression).
  axe-core is loaded from the official CDN at runtime — no local npm dep.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core import url_security
from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

_AXE_CDN_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.8.4/axe.min.js"
_DEFAULT_TAGS = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa")
_DEFAULT_WAIT_MS = 1500
_MAX_WAIT_MS = 8000
_MAX_VIOLATIONS = 30
_MAX_NODES_PER_VIOLATION = 5
_NODE_HTML_TRUNCATE = 400
_NODE_FAILURE_SUMMARY_CHARS = 600
_NODE_TARGET_KEEP = 3
_VIOLATION_HELP_CHARS = 300

_VIEWPORT_WIDTH_DEFAULT = 1280
_VIEWPORT_HEIGHT_DEFAULT = 800
_VIEWPORT_WIDTH_MIN = 320
_VIEWPORT_WIDTH_MAX = 3840
_VIEWPORT_HEIGHT_MIN = 240
_VIEWPORT_HEIGHT_MAX = 2160

_PLAYWRIGHT_NAV_TIMEOUT_MS = 20_000
_PLAYWRIGHT_IDLE_TIMEOUT_MS = 10_000

_USER_AGENT = "Aztea-Accessibility-Auditor/1.0 (axe-core; for authorized auditing)"

_AXE_RUN_JS = """
async (tags) => {
  try {
    const res = await axe.run(document, { runOnly: { type: 'tag', values: tags } });
    return { ok: true, result: res };
  } catch (err) {
    return { ok: false, error: String(err && err.message || err) };
  }
}
"""



def _install_request_guard(context: Any) -> None:  # noqa: ANN401
    """Block in-page navigations that target private/loopback hosts."""

    def _guard(route: Any) -> None:  # noqa: ANN401
        try:
            url_security.validate_outbound_url(route.request.url, "url")
        except Exception:
            route.abort()
            return
        route.continue_()

    context.route("**/*", _guard)


def _summarize_node(node: dict[str, Any]) -> dict[str, Any]:
    """Pure: shape an axe-core ``nodes[]`` entry into the agent's compact form."""
    target_raw = node.get("target") or []
    target = [str(t) for t in target_raw if isinstance(t, (str, int))][:_NODE_TARGET_KEEP]
    html = str(node.get("html") or "")
    if len(html) > _NODE_HTML_TRUNCATE:
        html = html[:_NODE_HTML_TRUNCATE] + "…"
    return {
        "target": target,
        "html": html,
        "failure_summary": str(node.get("failureSummary") or "")[:_NODE_FAILURE_SUMMARY_CHARS],
    }


def _normalize_tags(raw_tags: Any) -> list[str]:
    """Pure: coerce caller tags to a clean list, falling back to the WCAG defaults."""
    if raw_tags is None or not isinstance(raw_tags, list) or not raw_tags:
        return list(_DEFAULT_TAGS)
    cleaned = [str(t).strip() for t in raw_tags if str(t).strip()]
    return cleaned or list(_DEFAULT_TAGS)


def _normalize_viewport(vp: Any) -> tuple[int, int]:
    """Pure: clamp viewport dims to allowed range; fall back to defaults on parse error."""
    if not isinstance(vp, dict):
        vp = {}
    try:
        width = max(_VIEWPORT_WIDTH_MIN, min(int(vp.get("width") or _VIEWPORT_WIDTH_DEFAULT), _VIEWPORT_WIDTH_MAX))
        height = max(_VIEWPORT_HEIGHT_MIN, min(int(vp.get("height") or _VIEWPORT_HEIGHT_DEFAULT), _VIEWPORT_HEIGHT_MAX))
        return width, height
    except (TypeError, ValueError):
        return _VIEWPORT_WIDTH_DEFAULT, _VIEWPORT_HEIGHT_DEFAULT


def _normalize_wait_ms(raw: Any) -> int:
    """Pure: coerce ``wait_ms`` to an int in [0, _MAX_WAIT_MS]."""
    try:
        wait_ms = int(raw or _DEFAULT_WAIT_MS)
    except (TypeError, ValueError):
        wait_ms = _DEFAULT_WAIT_MS
    return max(0, min(wait_ms, _MAX_WAIT_MS))


def _aggregate_violations(raw_violations: list) -> tuple[list[dict], dict[str, int]]:
    """Pure: project axe-core violations into agent shape and tally impact counts."""
    impact_counts = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0}
    violations_out: list[dict[str, Any]] = []
    for v in raw_violations[:_MAX_VIOLATIONS]:
        if not isinstance(v, dict):
            continue
        impact = str(v.get("impact") or "minor").lower()
        if impact in impact_counts:
            impact_counts[impact] += 1
        nodes_raw = v.get("nodes") or []
        nodes = [
            _summarize_node(n)
            for n in nodes_raw[:_MAX_NODES_PER_VIOLATION]
            if isinstance(n, dict)
        ]
        violations_out.append({
            "id": str(v.get("id") or ""),
            "impact": impact,
            "tags": [str(t) for t in (v.get("tags") or []) if isinstance(t, str)],
            "help": str(v.get("help") or "")[:_VIOLATION_HELP_CHARS],
            "help_url": str(v.get("helpUrl") or ""),
            "node_count": len(nodes_raw),
            "nodes": nodes,
        })
    return violations_out, impact_counts


def _build_response(
    *, url: str, final_url: str, page_title: str, axe_result: dict,
    elapsed_ms: int,
) -> dict[str, Any]:
    """Pure: assemble the response envelope from a successful axe-core run."""
    raw_violations = axe_result.get("violations") or []
    raw_passes = axe_result.get("passes") or []
    raw_incomplete = axe_result.get("incomplete") or []
    test_engine = axe_result.get("testEngine") or {}
    violations_out, impact_counts = _aggregate_violations(raw_violations)
    return {
        "url": url,
        "final_url": final_url,
        "page_title": page_title,
        "axe_version": str(test_engine.get("version") or ""),
        "test_engine": str(test_engine.get("name") or "axe-core"),
        "violations": violations_out,
        "totals": {
            "violations": len(raw_violations),
            **impact_counts,
            "passes": len(raw_passes),
            "incomplete": len(raw_incomplete),
        },
        "execution_time_ms": elapsed_ms,
        "billing_units_actual": 1,
    }


def _normalize_inputs(payload: dict[str, Any]) -> dict[str, Any] | tuple:
    """Pure: validate and shape ``payload``. Returns ``(url, tags, vp, wait_ms)`` or an error envelope."""
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("accessibility_auditor.missing_url", "url is required")
    try:
        url = url_security.validate_outbound_url(raw_url, "url")
    except ValueError as exc:
        return _err("accessibility_auditor.invalid_url", str(exc))
    return (
        url,
        _normalize_tags(payload.get("tags")),
        _normalize_viewport(payload.get("viewport")),
        _normalize_wait_ms(payload.get("wait_ms")),
    )


def _is_chromium_missing_error(exc: BaseException) -> bool:
    """Pure: True when a Playwright launch failure means Chromium isn't provisioned."""
    msg = str(exc)
    return "Executable doesn't exist" in msg or "playwright install" in msg


def _wait_for_settle(page: Any, wait_ms: int) -> None:
    """Side-effect: best-effort networkidle wait + caller-specified post-load delay.

    Why: long-polling sites never hit networkidle; logging at debug keeps the
    failure visible without spamming WARN.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=_PLAYWRIGHT_IDLE_TIMEOUT_MS)
    except Exception:
        _LOG.debug("networkidle did not fire; continuing after fixed wait", exc_info=True)
    if wait_ms:
        page.wait_for_timeout(wait_ms)


def _run_axe_in_page(page: Any, tags: list[str]) -> dict[str, Any] | dict:
    """Side-effect: inject axe-core and run it against the page; returns axe result or error envelope."""
    try:
        page.add_script_tag(url=_AXE_CDN_URL)
    except Exception as exc:
        return _err(
            "accessibility_auditor.axe_load_failed",
            f"Could not load axe-core script: {exc}",
        )
    try:
        return page.evaluate(_AXE_RUN_JS, tags)
    except Exception as exc:
        return _err(
            "accessibility_auditor.axe_eval_failed",
            f"axe.run() failed: {type(exc).__name__}: {exc}",
        )


def _import_playwright() -> Any:
    """Side-effect: import Playwright lazily; returns the module or an error envelope.

    Why (rule 11): Playwright is heavy and absent on workers without
    browser provisioning, so the import is deliberately deferred.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
        return sync_playwright
    except ImportError:
        return _err(
            "accessibility_auditor.tool_unavailable",
            "playwright is not installed on this executor. Install with: "
            "pip install playwright && playwright install chromium",
        )


def _launch_browser(pw: Any) -> Any:
    """Side-effect: launch headless Chromium. Returns the browser or an error envelope."""
    try:
        return pw.chromium.launch(headless=True)
    except Exception as launch_exc:
        if _is_chromium_missing_error(launch_exc):
            return _err(
                "tool_unavailable",
                "Headless Chromium is not provisioned on this worker. "
                "Run `playwright install chromium`. Call was not billed.",
            )
        raise


def _navigate_and_capture(
    page: Any, url: str, tags: list[str], wait_ms: int
) -> dict[str, Any]:
    """Side-effect: navigate, wait, run axe; returns the per-page session payload or error envelope."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_PLAYWRIGHT_NAV_TIMEOUT_MS)
    except Exception as exc:
        return _err(
            "accessibility_auditor.navigation_failed",
            f"Failed to load page: {type(exc).__name__}: {exc}",
        )
    _wait_for_settle(page, wait_ms)
    axe_result = _run_axe_in_page(page, tags)
    if isinstance(axe_result, dict) and "error" in axe_result:
        return axe_result
    if not isinstance(axe_result, dict) or not axe_result.get("ok"):
        return _err(
            "accessibility_auditor.axe_failed",
            str((axe_result or {}).get("error") or "axe-core returned no result"),
        )
    return {
        "axe_result": axe_result.get("result") or {},
        "page_title": page.title() or "",
        "final_url": page.url,
    }


def _audit_with_chromium(
    url: str, tags: list[str], viewport: tuple[int, int], wait_ms: int
) -> dict[str, Any]:
    """Side-effect: orchestrate one Chromium session for ``run``. Returns a session dict or error envelope."""
    sync_playwright = _import_playwright()
    if isinstance(sync_playwright, dict):
        return sync_playwright  # error envelope
    width, height = viewport
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        if isinstance(browser, dict):
            return browser  # error envelope
        context = browser.new_context(
            viewport={"width": width, "height": height}, user_agent=_USER_AGENT,
        )
        _install_request_guard(context)
        page = context.new_page()
        try:
            return _navigate_and_capture(page, url, tags, wait_ms)
        finally:
            context.close()
            browser.close()


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Run axe-core WCAG checks against a URL using a real Chromium browser.

    Why: a hosted browser is the only way to evaluate post-JS DOM state;
    we keep one session alive per call to amortise the heavy launch.
    """
    normalized = _normalize_inputs(payload)
    if isinstance(normalized, dict):
        return normalized  # error envelope
    url, tags, viewport, wait_ms = normalized
    t_start = time.monotonic()
    try:
        session = _audit_with_chromium(url, tags, viewport, wait_ms)
    except Exception as exc:
        return _err(
            "accessibility_auditor.browser_failed",
            f"Browser session failed: {type(exc).__name__}: {exc}",
        )
    if "error" in session:
        return session  # error envelope
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    return _build_response(
        url=url,
        final_url=session["final_url"],
        page_title=session["page_title"],
        axe_result=session["axe_result"],
        elapsed_ms=elapsed_ms,
    )
