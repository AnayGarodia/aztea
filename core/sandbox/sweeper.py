"""Lifetime sweeper: hard max, idle kill, auto-snapshot.

# OWNS: the periodic check that retires expired sandboxes, auto-snapshots
#       running ones, and exposes the per-sandbox cost summary.
# NOT OWNS: snapshot mechanics (see snapshots.py); lifecycle teardown
#           (see lifecycle.py); receipts (see receipts.py).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from core.sandbox.models import now_unix
from core.sandbox.state import SandboxState, list_all

_LOG = logging.getLogger("aztea.sandbox.sweeper")
_SWEEPER_INTERVAL_S = 60
_SWEEPER_THREAD: threading.Thread | None = None
_SWEEPER_STOP_EVENT: threading.Event | None = None


def maybe_start_sweeper() -> None:
    """Side-effect: start the background sweeper once per process.

    Why: in production this lives next to the existing background-loop
    machinery in part_004.py. We keep it module-local so tests don't pull
    in the whole server.
    """
    global _SWEEPER_THREAD, _SWEEPER_STOP_EVENT
    if _SWEEPER_THREAD is not None and _SWEEPER_THREAD.is_alive():
        return
    _SWEEPER_STOP_EVENT = threading.Event()
    _SWEEPER_THREAD = threading.Thread(
        target=_sweeper_loop, name="aztea-sandbox-sweeper", daemon=True
    )
    _SWEEPER_THREAD.start()


def stop_sweeper() -> None:
    """Side-effect: signal the sweeper to exit (tests + shutdown only)."""
    global _SWEEPER_STOP_EVENT, _SWEEPER_THREAD
    if _SWEEPER_STOP_EVENT is not None:
        _SWEEPER_STOP_EVENT.set()
    if _SWEEPER_THREAD is not None:
        _SWEEPER_THREAD.join(timeout=2)
    _SWEEPER_THREAD = None
    _SWEEPER_STOP_EVENT = None


def cost_summary(state: SandboxState) -> dict[str, Any]:
    """Pure-ish: cost-so-far for ``state``, including the per-sandbox cap snapshot.

    Audit 2026-05-17 gap #5: pre-fix this returned only minutes-used +
    a placeholder billing_notice. Now we also surface the per-sandbox
    spending cap and the cents accumulated so a caller can spot a
    runaway sandbox before it hits the cap.
    """
    from core.sandbox import spending as _spending

    minutes_used = max(0, (now_unix() - state.created_at) // 60)
    snapshot = _spending.snapshot(state.sandbox_id)
    return {
        "sandbox_id": state.sandbox_id,
        "minutes_used": minutes_used,
        "minutes_remaining": max(0, state.lifetime.max_minutes - minutes_used),
        "snapshot_count": len(state.snapshot_chain),
        "size": state.size,
        "spending": snapshot,
        "billing_notice": (
            "Per-sandbox soft cap is enforced at the engine boundary (gap "
            "#5). Wallet-backed atomic billing arrives with the "
            "caller_api_keys table follow-up."
        ),
    }


def _sweeper_loop() -> None:
    """Side-effect: iterate every minute, retire/snapshot/idle-kill."""
    assert _SWEEPER_STOP_EVENT is not None
    while not _SWEEPER_STOP_EVENT.is_set():
        try:
            _sweep_once()
        except Exception:
            _LOG.exception("sandbox sweeper iteration failed")
        _SWEEPER_STOP_EVENT.wait(_SWEEPER_INTERVAL_S)


def _sweep_once() -> None:
    """Side-effect: walk the registry and apply lifetime policies."""
    now = now_unix()
    for state in list_all():
        try:
            _apply_policies(state, now)
        except Exception:
            _LOG.exception("sweeper apply_policies failed for %s", state.sandbox_id)


def _apply_policies(state: SandboxState, now: int) -> None:
    """Side-effect: decide whether to auto-snapshot, idle-kill, or hard-stop."""
    if state.status not in ("ready", "running"):
        return
    idle_seconds = now - state.last_activity_at
    if idle_seconds > state.lifetime.idle_kill_minutes * 60:
        _LOG.info("idle-kill: %s (idle for %ds)", state.sandbox_id, idle_seconds)
        _suspend(state, reason="idle_kill")
        return
    if now >= state.expires_at:
        _LOG.info("max-lifetime reached: %s", state.sandbox_id)
        _suspend(state, reason="max_lifetime")
        return
    snap_due = state.last_snapshot_at + state.lifetime.auto_snapshot_every_minutes * 60
    if now >= snap_due:
        _auto_snapshot(state)


def _auto_snapshot(state: SandboxState) -> None:
    """Side-effect: take a periodic snapshot; degrade silently on failure."""
    from core.sandbox.snapshots import snapshot as snapshot_action

    try:
        snapshot_action({"sandbox_id": state.sandbox_id, "reason": "auto"})
    except Exception:
        _LOG.exception("auto-snapshot failed for %s", state.sandbox_id)


def _suspend(state: SandboxState, *, reason: str) -> None:
    """Side-effect: stop the sandbox after one final snapshot.

    Why: ``suspended`` lets a caller ``sandbox_resume`` to pick up where
    they left off; we don't fully ``stop`` so the snapshot chain stays
    addressable from the original sandbox_id.
    """
    from core.sandbox.lifecycle import stop as stop_action

    try:
        stop_action({"sandbox_id": state.sandbox_id, "final_snapshot": True})
        state.status = "suspended"
        state.failure_reason = reason
    except Exception:
        _LOG.exception("suspend failed for %s", state.sandbox_id)
