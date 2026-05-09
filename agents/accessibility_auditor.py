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

import time
from typing import Any

from core import url_security

_AXE_CDN_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.8.4/axe.min.js"
_DEFAULT_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]
_DEFAULT_WAIT_MS = 1500
_MAX_WAIT_MS = 8000
_MAX_VIOLATIONS = 30
_MAX_NODES_PER_VIOLATION = 5
_NODE_HTML_TRUNCATE = 400


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


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
    target_raw = node.get("target") or []
    target = [str(t) for t in target_raw if isinstance(t, (str, int))][:3]
    html = str(node.get("html") or "")
    if len(html) > _NODE_HTML_TRUNCATE:
        html = html[:_NODE_HTML_TRUNCATE] + "…"
    return {
        "target": target,
        "html": html,
        "failure_summary": str(node.get("failureSummary") or "")[:600],
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Run axe-core WCAG checks against a URL using a real Chromium browser.

    Returns structured violations grouped by rule, with affected DOM nodes
    truncated for transport. Single-shot — costs one billing unit per call.
    Honors caller-supplied tag filters; defaults to the WCAG-2.1-AA bundle.
    """
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("accessibility_auditor.missing_url", "url is required")

    try:
        url = url_security.validate_outbound_url(raw_url, "url")
    except ValueError as exc:
        return _err("accessibility_auditor.invalid_url", str(exc))

    raw_tags = payload.get("tags")
    if raw_tags is None or not isinstance(raw_tags, list) or not raw_tags:
        tags = list(_DEFAULT_TAGS)
    else:
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        if not tags:
            tags = list(_DEFAULT_TAGS)

    vp = payload.get("viewport") or {}
    try:
        vp_width = max(320, min(int(vp.get("width") or 1280), 3840))
        vp_height = max(240, min(int(vp.get("height") or 800), 2160))
    except (TypeError, ValueError):
        vp_width, vp_height = 1280, 800

    try:
        wait_ms = int(payload.get("wait_ms") or _DEFAULT_WAIT_MS)
    except (TypeError, ValueError):
        wait_ms = _DEFAULT_WAIT_MS
    wait_ms = max(0, min(wait_ms, _MAX_WAIT_MS))

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
    except ImportError:
        return _err(
            "accessibility_auditor.tool_unavailable",
            "playwright is not installed on this executor. Install with: "
            "pip install playwright && playwright install chromium",
        )

    t_start = time.monotonic()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as launch_exc:  # noqa: BLE001
                msg = str(launch_exc)
                if "Executable doesn't exist" in msg or "playwright install" in msg:
                    return _err(
                        "tool_unavailable",
                        "Headless Chromium is not provisioned on this worker. "
                        "Run `playwright install chromium`. Call was not billed.",
                    )
                raise

            context = browser.new_context(
                viewport={"width": vp_width, "height": vp_height},
                user_agent="Aztea-Accessibility-Auditor/1.0 (axe-core; for authorized auditing)",
            )
            _install_request_guard(context)
            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            except Exception as exc:
                context.close()
                browser.close()
                return _err(
                    "accessibility_auditor.navigation_failed",
                    f"Failed to load page: {type(exc).__name__}: {exc}",
                )

            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                # networkidle is best-effort; some sites keep long-poll connections open
                pass
            if wait_ms:
                page.wait_for_timeout(wait_ms)

            page_title = page.title() or ""
            final_url = page.url

            # Inject axe-core from CDN — keeps the image small and gets
            # security updates for free. The request_guard above will block
            # the load if the CDN somehow resolves to a private IP.
            try:
                page.add_script_tag(url=_AXE_CDN_URL)
            except Exception as exc:
                context.close()
                browser.close()
                return _err(
                    "accessibility_auditor.axe_load_failed",
                    f"Could not load axe-core script: {exc}",
                )

            try:
                # axe.run returns a Promise; Playwright awaits it automatically.
                axe_result = page.evaluate(
                    """
                    async (tags) => {
                      try {
                        const res = await axe.run(document, { runOnly: { type: 'tag', values: tags } });
                        return { ok: true, result: res };
                      } catch (err) {
                        return { ok: false, error: String(err && err.message || err) };
                      }
                    }
                    """,
                    tags,
                )
            except Exception as exc:
                context.close()
                browser.close()
                return _err(
                    "accessibility_auditor.axe_eval_failed",
                    f"axe.run() failed: {type(exc).__name__}: {exc}",
                )

            context.close()
            browser.close()
    except Exception as exc:
        return _err(
            "accessibility_auditor.browser_failed",
            f"Browser session failed: {type(exc).__name__}: {exc}",
        )

    if not isinstance(axe_result, dict) or not axe_result.get("ok"):
        return _err(
            "accessibility_auditor.axe_failed",
            str((axe_result or {}).get("error") or "axe-core returned no result"),
        )

    result = axe_result.get("result") or {}
    raw_violations = result.get("violations") or []
    raw_passes = result.get("passes") or []
    raw_incomplete = result.get("incomplete") or []

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
        violations_out.append(
            {
                "id": str(v.get("id") or ""),
                "impact": impact,
                "tags": [str(t) for t in (v.get("tags") or []) if isinstance(t, str)],
                "help": str(v.get("help") or "")[:300],
                "help_url": str(v.get("helpUrl") or ""),
                "node_count": len(nodes_raw),
                "nodes": nodes,
            }
        )

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    test_engine = result.get("testEngine") or {}

    return {
        "url": url,
        "final_url": final_url,
        "page_title": page_title,
        "axe_version": str(test_engine.get("version") or ""),
        "test_engine": str(test_engine.get("name") or "axe-core"),
        "violations": violations_out,
        "totals": {
            "violations": len(raw_violations),
            "critical": impact_counts["critical"],
            "serious": impact_counts["serious"],
            "moderate": impact_counts["moderate"],
            "minor": impact_counts["minor"],
            "passes": len(raw_passes),
            "incomplete": len(raw_incomplete),
        },
        "execution_time_ms": elapsed_ms,
        "billing_units_actual": 1,
    }
