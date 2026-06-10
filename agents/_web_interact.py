"""Interact-then-reveal + gated credential login for the write-web.

# OWNS: a BOUNDED sequence of content-revealing interactions (click/fill/select/
#        scroll/wait) in headless Chromium (perform_interaction, the SAFE E1 tier,
#        no credentials), and the gated credential-injection variant that logs into a
#        user's account before revealing (perform_login).
# NOT OWNS: the escrowed commit path (web_actor._commit), mandates, the read agent,
#           credential storage/decryption (core.credential_vault).
# INVARIANTS:
#   * Bounded: <= _MAX_STEPS steps, per-step timeout, total wall budget, the SSRF
#     route guard on every in-page request, headless only. Moves no money.
#   * perform_interaction holds NO credentials. perform_login injects a decrypted
#     Credential that the GATED caller supplies; the secret value is sourced ONLY from
#     the vault, never from caller-supplied step values, and is never logged.
#   * A failing step STOPS the sequence with a structured result (how far it got),
#     never a raw exception into the worker.
# DECISIONS:
#   * Lives in the OFF-by-default write-web agent (web_actor), NOT site_navigator, so
#     the read agent keeps zero interaction code — a coerced read path still can't act.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import struct
import time
from typing import Any

from agents._contracts import agent_error as _err
from core import url_security
from core.web import stealth_browser

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
# Total wall-clock cap across all steps: per-step timeouts alone could still hold a
# worker ~84s, so the sequence stops once this elapses.
_TOTAL_BUDGET_S = 30.0
_UA = "Aztea-Web-Actor/1.0 (headless; interact-then-reveal)"

# Best-effort login-field locators (used only on the gated credential path). A
# missing field is skipped — the caller's steps handle the submit. The values come
# from the vault, never from caller step values.
_USERNAME_SELECTORS = (
    "input[type=email], input[autocomplete=username], input[name=username], "
    "input[name=email], input[id=username], input[id=email]"
)
_PASSWORD_SELECTORS = "input[type=password]"
_OTP_SELECTORS = "input[autocomplete=one-time-code], input[name*=otp], input[name*=code], input[id*=otp]"
_MAX_INJECT_COOKIES = 50
_TOTP_PERIOD_S = 30
_TOTP_DIGITS = 6


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


class ElementNotFound(Exception):
    """No visible element matched the target across any resolution strategy.

    Raised by _apply_step so a missed element surfaces as a clear, bounded step result
    (the "where it lags" signal) rather than a generic timeout.
    """


# Roles tried for accessible-name matching, most-likely first. get_by_role matches the
# element's ACCESSIBLE name (aria-label, aria-labelledby, associated label, or text), so
# this one strategy already covers several of Sisyphus's label sources at once.
_INTERACTIVE_ROLES = (
    "button", "link", "textbox", "combobox", "checkbox",
    "radio", "tab", "menuitem", "option", "switch",
)


def _looks_like_css(target: str) -> bool:
    """Pure: heuristic for a target the caller meant as a raw CSS selector."""
    return bool(target) and (
        target[0] in "#.["
        or " > " in target
        or target.startswith(("input", "button", "a[", "select", "textarea"))
    )


def _candidate_locators(page: Any, target: str) -> list[Any]:
    """Build an ordered list of locator strategies for `target`, most precise first.

    Mirrors the multi-source label match Sisyphus's scan computes — accessible name
    (aria-label / aria-labelledby / label / text via get_by_role), associated <label>,
    placeholder, visible text, title, alt, then attribute fallbacks (aria-label, name,
    id, value). web_actor's old path used only get_by_text (click) and get_by_label
    (fill), which missed icon buttons, placeholder-only inputs, and name/id-only fields.
    """
    cands: list[Any] = []
    if _looks_like_css(target):
        cands.append(page.locator(target))
    for role in _INTERACTIVE_ROLES:
        cands.append(page.get_by_role(role, name=target, exact=False))
    cands.append(page.get_by_label(target, exact=False))
    cands.append(page.get_by_placeholder(target, exact=False))
    cands.append(page.get_by_text(target, exact=False))
    cands.append(page.get_by_title(target, exact=False))
    cands.append(page.get_by_alt_text(target, exact=False))
    esc = target.replace('"', '\\"')
    for css in (f'[aria-label*="{esc}" i]', f'[placeholder*="{esc}" i]',
                f'[name="{esc}"]', f'[id="{esc}"]', f'[value*="{esc}" i]'):
        cands.append(page.locator(css))
    return cands


def _resolve_locator(page: Any, target: str) -> Any:
    """Return the first locator resolving to a VISIBLE element, or None.

    The robust element-detection fix: try each strategy in order and keep the first
    whose visible-filtered match exists. A strategy that errors (unsupported role,
    malformed selector) is skipped, not fatal.
    """
    for loc in _candidate_locators(page, target):
        try:
            visible_first = loc.filter(visible=True).first
            if visible_first.count() > 0:
                return visible_first
        except Exception:  # noqa: BLE001 — a bad strategy just doesn't match; try the next
            continue
    return None


def _apply_step(page: Any, step: dict[str, str]) -> None:
    """Side-effect: apply one interaction step. Raises on failure (caller records it).

    Element-bearing actions resolve their target via the multi-strategy _resolve_locator,
    scroll it into view, then act — far more robust than the old single text/label match.
    """
    action = step["action"]
    if action == "scroll":
        page.mouse.wheel(0, _SCROLL_PX)
        return
    if action == "wait":
        page.wait_for_timeout(_wait_ms(step["value"]))
        return
    loc = _resolve_locator(page, step["target"])
    if loc is None:
        raise ElementNotFound(f"no visible element matched {step['target']!r}")
    loc.scroll_into_view_if_needed(timeout=_STEP_TIMEOUT_MS)
    if action == "click":
        loc.click(timeout=_STEP_TIMEOUT_MS)
    elif action == "fill":
        loc.fill(step["value"], timeout=_STEP_TIMEOUT_MS)
    elif action == "select":
        loc.select_option(step["value"], timeout=_STEP_TIMEOUT_MS)


def _open_context(pw: Any) -> tuple[Any, Any]:
    """Side-effect: launch a stealth-aware headless browser + context with the SSRF
    route guard installed. Returns (browser, context) for the caller to close."""
    browser = pw.chromium.launch(headless=True, **stealth_browser.launch_kwargs())
    context = browser.new_context(**stealth_browser.context_kwargs(honest_ua=_UA))
    _install_request_guard(context)
    return browser, context


def _drive_and_reveal(page: Any, steps: list[dict[str, str]]) -> dict[str, Any]:
    """Side-effect: apply the bounded steps (under the total budget) then return the
    revealed page. Shared by interact + login. A failing step stops with a structured
    result; the per-step budget is measured from the first step (after navigation)."""
    completed = 0
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
                f"{type(exc).__name__}: {str(exc)[:120]}. "
                f"Completed {completed}/{len(steps)} steps.",
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


def perform_interaction(pw: Any, url: str, steps: list[dict[str, str]]) -> dict[str, Any]:
    """Side-effect: drive a bounded interaction sequence and return the revealed content.

    The SAFE E1 tier: no credentials. Returns {phase, final_url, title, text,
    steps_completed, steps_total} or a structured error envelope.
    """
    browser, context = _open_context(pw)
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        return _drive_and_reveal(page, steps)
    finally:
        context.close()
        browser.close()


def _totp_now(secret_b32: str) -> str:
    """Pure: the current RFC-6238 TOTP code (SHA1, 30s, 6 digits) for a base32 secret."""
    raw = secret_b32.strip().replace(" ", "").upper()
    key = base64.b32decode(raw + "=" * (-len(raw) % 8))
    counter = int(time.time()) // _TOTP_PERIOD_S
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF) % (10 ** _TOTP_DIGITS)
    return str(code).rjust(_TOTP_DIGITS, "0")


def _try_fill_first(page: Any, selector: str, value: str) -> None:
    """Side-effect: fill the first matching input. A missing field is fine (the caller's
    steps may handle login differently) — skip it rather than failing the whole run."""
    if not value:
        return
    try:
        page.locator(selector).first.fill(value, timeout=_STEP_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — absent/ambiguous field; not fatal
        _LOG.debug("login field not found for selector", exc_info=True)


def _inject_cookies(context: Any, cookies: list[dict[str, Any]]) -> None:
    """Side-effect: inject session cookies (a logged-in session). Only well-formed,
    domain-scoped cookies are added; the count is capped to bound abuse."""
    safe = [
        c for c in cookies[:_MAX_INJECT_COOKIES]
        if isinstance(c, dict) and c.get("name") and c.get("domain")
    ]
    if safe:
        context.add_cookies(safe)


def perform_login(pw: Any, url: str, credential: Any, steps: list[dict[str, str]]) -> dict[str, Any]:
    """Side-effect (GATED path): inject a decrypted credential, navigate, drive the
    bounded steps, and return the revealed page.

    Cookie credentials inject a logged-in session directly; password/totp credentials
    fill the obvious login inputs and let `steps` submit. The secret value is sourced
    ONLY from `credential` (the vault), never from caller step values, and is never
    logged. The caller is responsible for credential.scrub() afterwards.
    """
    browser, context = _open_context(pw)
    try:
        if credential.kind == "cookies":
            _inject_cookies(context, credential.cookies)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        if credential.kind in ("password", "totp"):
            _try_fill_first(page, _USERNAME_SELECTORS, credential.username)
            _try_fill_first(page, _PASSWORD_SELECTORS, credential.password)
        if credential.kind == "totp" and credential.totp_secret:
            _try_fill_first(page, _OTP_SELECTORS, _totp_now(credential.totp_secret))
        return _drive_and_reveal(page, steps)
    finally:
        context.close()
        browser.close()
