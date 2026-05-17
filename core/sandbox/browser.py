"""Sandbox-scoped headless Chromium sessions.

# OWNS: sandbox_browser_session + sandbox_browser_navigate +
#       sandbox_browser_screenshot + sandbox_browser_console_logs.
#       The session pool is module-local; each session belongs to one
#       sandbox_id with its own cookie jar (isolated from other sessions).
# NOT OWNS: click/fill/eval/a11y/axe/lighthouse/record/replay — these
#           remain stubs that share the same Playwright pool follow-up
#           issue, so filling them is incremental (no infra change needed).
# INVARIANTS:
#   * Playwright is imported lazily on first use so test fixtures and
#     workers without chromium installed still import the module cleanly.
#   * Every session has a per-sandbox cookie + storage state directory.
#   * Sessions are evicted when their parent sandbox is stopped.
"""

from __future__ import annotations

import base64
import logging
import secrets
import threading
from typing import Any

from core.sandbox.models import SandboxInvalidInput
from core.sandbox.state import SandboxState, get, sandbox_dir

_LOG = logging.getLogger("aztea.sandbox.browser")
_NAV_TIMEOUT_MS = 15_000
_DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
_MAX_SESSIONS_PER_SANDBOX = 4
_CONSOLE_LOG_LIMIT = 200


class _SessionEntry:
    """Holds the per-session Playwright resources and console log buffer."""

    def __init__(self, session_id: str, sandbox_id: str) -> None:
        self.session_id = session_id
        self.sandbox_id = sandbox_id
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.console_logs: list[dict[str, Any]] = []


_SESSIONS: dict[str, _SessionEntry] = {}
_SESSIONS_LOCK = threading.RLock()


def session_open(payload: dict[str, Any]) -> dict[str, Any]:
    """Start a new headless Chromium session bound to this sandbox.

    Returns ``{session_id, cdp_url}``. The session lives until the sandbox
    stops or :func:`session_close` is called explicitly.
    """
    state = _require(payload)
    sessions_for_sandbox = [
        entry for entry in _SESSIONS.values() if entry.sandbox_id == state.sandbox_id
    ]
    if len(sessions_for_sandbox) >= _MAX_SESSIONS_PER_SANDBOX:
        raise SandboxInvalidInput(
            f"sandbox '{state.sandbox_id}' already has "
            f"{_MAX_SESSIONS_PER_SANDBOX} open browser sessions; close one "
            "or stop the sandbox before starting another"
        )
    viewport = dict(_DEFAULT_VIEWPORT)
    viewport.update(payload.get("viewport") or {})
    storage_dir = sandbox_dir(state.sandbox_id) / "browser" / "sessions"
    storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    session_id = f"sess_{secrets.token_hex(6)}"
    sync_playwright = _import_playwright()
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=bool(payload.get("headless", True)))
        context = browser.new_context(
            viewport={"width": int(viewport["width"]), "height": int(viewport["height"])},
            storage_state=None,
        )
        page = context.new_page()
    except Exception:
        pw.stop()
        raise
    entry = _SessionEntry(session_id, state.sandbox_id)
    entry.browser = browser
    entry.context = context
    entry.page = page
    entry._playwright = pw  # type: ignore[attr-defined]
    _attach_console_listener(entry)
    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = entry
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "session_id": session_id,
        "viewport": viewport,
        "storage_state_path": str(storage_dir / f"{session_id}.json"),
        "cdp_url": None,  # CDP exposure is the follow-up issue; not in this slice.
    }


def session_close(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    session_id = _resolve_session_id(payload)
    entry = _SESSIONS.get(session_id)
    if entry is None or entry.sandbox_id != state.sandbox_id:
        raise SandboxInvalidInput(
            f"session '{session_id}' not found for sandbox '{state.sandbox_id}'"
        )
    _teardown_entry(entry)
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session_id, None)
    return {"sandbox_id": state.sandbox_id, "session_id": session_id, "closed": True}


def navigate(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    entry = _require_session(state, payload)
    url = str(payload.get("url") or "").strip()
    if not url:
        raise SandboxInvalidInput("url is required for sandbox_browser_navigate")
    wait_until = str(payload.get("wait_until") or "load").strip().lower()
    if wait_until not in {"load", "domcontentloaded", "networkidle", "commit"}:
        wait_until = "load"
    response = entry.page.goto(
        url, wait_until=wait_until, timeout=_NAV_TIMEOUT_MS,
    )
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "session_id": entry.session_id,
        "url": entry.page.url,
        "title": entry.page.title(),
        "status": getattr(response, "status", None) if response else None,
    }


def screenshot(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    entry = _require_session(state, payload)
    full_page = bool(payload.get("full_page", True))
    png_bytes = entry.page.screenshot(full_page=full_page)
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "session_id": entry.session_id,
        "mime": "image/png",
        "size_bytes": len(png_bytes),
        "screenshot_b64": base64.b64encode(png_bytes).decode("ascii"),
        "full_page": full_page,
    }


def console_logs(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    entry = _require_session(state, payload)
    clear = bool(payload.get("clear", False))
    out = list(entry.console_logs)
    if clear:
        entry.console_logs.clear()
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "session_id": entry.session_id,
        "logs": out,
        "cleared": clear,
    }


def evict_for_sandbox(sandbox_id: str) -> int:
    """Stop and forget every session belonging to ``sandbox_id``.

    Why: ``lifecycle.stop`` calls this so a sandbox teardown also closes
    its browser sessions — without this Playwright would leak chromium
    children after the host containers go away.
    """
    closed = 0
    with _SESSIONS_LOCK:
        ids = [sid for sid, e in _SESSIONS.items() if e.sandbox_id == sandbox_id]
        for sid in ids:
            entry = _SESSIONS.pop(sid)
            try:
                _teardown_entry(entry)
            except Exception:
                _LOG.exception("teardown browser session %s failed", sid)
            closed += 1
    return closed


def _import_playwright():
    """Side-effect: lazy import; clear error envelope when chromium is missing."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
    except ImportError as exc:
        raise SandboxInvalidInput(
            "playwright is not installed in this runtime. Install with: "
            "pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


def _attach_console_listener(entry: _SessionEntry) -> None:
    """Side-effect: register a Playwright listener that buffers console events."""

    def _on_console(msg: Any) -> None:
        if len(entry.console_logs) >= _CONSOLE_LOG_LIMIT:
            entry.console_logs.pop(0)
        try:
            entry.console_logs.append({
                "type": getattr(msg, "type", None),
                "text": getattr(msg, "text", None),
                "location": _location_dict(getattr(msg, "location", None)),
            })
        except Exception:
            _LOG.exception("console listener serialise failed")

    entry.page.on("console", _on_console)


def _location_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return {k: value.get(k) for k in ("url", "lineNumber", "columnNumber")}
    return {
        "url": getattr(value, "url", None),
        "lineNumber": getattr(value, "lineNumber", None),
        "columnNumber": getattr(value, "columnNumber", None),
    }


def _teardown_entry(entry: _SessionEntry) -> None:
    """Side-effect: best-effort Playwright teardown; never raises."""
    for label, target in (
        ("page", entry.page),
        ("context", entry.context),
        ("browser", entry.browser),
        ("playwright", getattr(entry, "_playwright", None)),
    ):
        if target is None:
            continue
        try:
            if label == "playwright":
                target.stop()
            else:
                target.close()
        except Exception:
            _LOG.debug("browser teardown step %s raised", label, exc_info=True)


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxInvalidInput(f"sandbox '{sandbox_id}' not active")
    return state


def _require_session(state: SandboxState, payload: dict[str, Any]) -> _SessionEntry:
    session_id = _resolve_session_id(payload)
    entry = _SESSIONS.get(session_id)
    if entry is None or entry.sandbox_id != state.sandbox_id:
        raise SandboxInvalidInput(
            f"session '{session_id}' not found for sandbox '{state.sandbox_id}' — "
            f"call sandbox_browser_session first"
        )
    return entry


def _resolve_session_id(payload: dict[str, Any]) -> str:
    sid = str((payload or {}).get("session_id") or "").strip()
    if not sid:
        raise SandboxInvalidInput("session_id is required")
    return sid
