"""Interact-then-reveal — the write-web's SAFE tier (E1). No money, no credentials.

# OWNS: a BOUNDED sequence of content-revealing interactions (click/fill/select/
#        scroll/wait) in headless Chromium, returning the revealed page content.
# NOT OWNS: the escrowed commit path (web_actor._commit), mandates, the read agent.
# INVARIANTS:
#   * Bounded: <= _MAX_STEPS steps, per-step timeout, the SSRF route guard on every
#     in-page request, headless only. Moves no money, holds no credentials.
#   * A failing step STOPS the sequence with a structured result (how far it got),
#     never a raw exception into the worker.
# DECISIONS:
#   * Lives in the OFF-by-default write-web agent (web_actor), NOT site_navigator, so
#     the read agent keeps zero interaction code — a coerced read path still can't act.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agents._contracts import agent_error as _err
from core import url_security

_LOG = logging.getLogger(__name__)

_MAX_STEPS = 10
_STEP_ACTIONS = ("click", "fill", "select", "scroll", "wait")
_TARGET_REQUIRED = ("click", "fill", "select")
_TARGET_MAX = 200
_VALUE_MAX = 400
_NAV_TIMEOUT_MS = 15_000
_STEP_TIMEOUT_MS = 8_000
_STEP_SETTLE_MS = 400
_MAX_WAIT_MS = 4_000
_SCROLL_PX = 1_200
_TEXT_TRUNCATE = 40_000
_TOTAL_BUDGET_S = 30.0  # total wall-clock cap across all steps (per-step timeouts alone
                        # could still hold a worker ~84s); the sequence stops once this elapses
_UA = "Aztea-Web-Actor/1.0 (headless; interact-then-reveal)"


def parse_steps(raw: Any) -> list[dict[str, str]]:
    """Pure: validate + bound the interaction steps. Raises ValueError on invalid input.

    Why fail-loud here: a malformed step list must never reach Chromium, and the cap
    keeps a hostile caller from driving an unbounded interaction loop on the worker.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("steps must be a non-empty list of interaction objects")
    if len(raw) > _MAX_STEPS:
        raise ValueError(f"too many steps ({len(raw)}); the cap is {_MAX_STEPS}")
    parsed: list[dict[str, str]] = []
    for i, step in enumerate(raw):
        if not isinstance(step, dict):
            raise ValueError(f"step {i} must be an object")
        action = str(step.get("action") or "").strip().lower()
        if action not in _STEP_ACTIONS:
            raise ValueError(f"step {i}: action must be one of {', '.join(_STEP_ACTIONS)}")
        target = str(step.get("target") or "").strip()
        if action in _TARGET_REQUIRED and not target:
            raise ValueError(f"step {i}: '{action}' requires a 'target' (the element's visible text or label)")
        parsed.append({
            "action": action,
            "target": target[:_TARGET_MAX],
            "value": str(step.get("value") or "")[:_VALUE_MAX],
        })
    return parsed


def _install_request_guard(context: Any) -> None:
    """Side-effect: abort in-page requests to blocked/private targets (mirrored SSRF guard)."""
    def _guard(route: Any) -> None:
        try:
            url_security.validate_outbound_url(route.request.url, "url")
        except Exception:  # noqa: BLE001 — any validation failure blocks the subrequest
            route.abort()
            return
        route.continue_()

    context.route("**/*", _guard)


def _wait_ms(value: str) -> int:
    """Pure: clamp a caller-supplied wait to [0, _MAX_WAIT_MS]; default 1000ms."""
    try:
        return max(0, min(int(value), _MAX_WAIT_MS))
    except (ValueError, TypeError):
        return 1_000


def _apply_step(page: Any, step: dict[str, str]) -> None:
    """Side-effect: apply one interaction step. Raises on failure (caller records it)."""
    action = step["action"]
    target = step["target"]
    if action == "click":
        page.get_by_text(target, exact=False).first.click(timeout=_STEP_TIMEOUT_MS)
    elif action == "fill":
        page.get_by_label(target, exact=False).first.fill(step["value"], timeout=_STEP_TIMEOUT_MS)
    elif action == "select":
        page.get_by_label(target, exact=False).first.select_option(step["value"], timeout=_STEP_TIMEOUT_MS)
    elif action == "scroll":
        page.mouse.wheel(0, _SCROLL_PX)
    elif action == "wait":
        page.wait_for_timeout(_wait_ms(step["value"]))


def perform_interaction(pw: Any, url: str, steps: list[dict[str, str]]) -> dict[str, Any]:
    """Side-effect: drive a bounded interaction sequence and return the revealed content.

    Returns {phase, final_url, title, text, steps_completed, steps_total} or a structured
    error envelope. A failing step stops the sequence and reports how far it got.
    """
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent=_UA)
    _install_request_guard(context)
    page = context.new_page()
    completed = 0
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        started = time.monotonic()
        for step in steps:
            if time.monotonic() - started > _TOTAL_BUDGET_S:
                return _err(
                    "web_actor.interaction_budget_exceeded",
                    f"interaction exceeded the {_TOTAL_BUDGET_S:.0f}s total budget after "
                    f"{completed}/{len(steps)} steps.",
                )
            try:
                _apply_step(page, step)
            except Exception as exc:  # noqa: BLE001 — bounded, structured stop (not a crash)
                return _err(
                    "web_actor.interaction_step_failed",
                    f"step {completed} ('{step['action']}' on '{step['target']}') failed: "
                    f"{type(exc).__name__}. Completed {completed}/{len(steps)} steps.",
                )
            completed += 1
            page.wait_for_timeout(_STEP_SETTLE_MS)
        try:
            text = page.locator("body").inner_text()[:_TEXT_TRUNCATE]
        except Exception:  # noqa: BLE001 — empty text degrades gracefully
            _LOG.debug("inner_text failed after interaction", exc_info=True)
            text = ""
        return {
            "phase": "interacted",
            "final_url": page.url,
            "title": page.title() or "",
            "text": text,
            "steps_completed": completed,
            "steps_total": len(steps),
            "degraded_mode": False,
        }
    finally:
        context.close()
        browser.close()
