"""Per-sandbox spending cap + batch pre-hold.

# OWNS: tracking accumulated cost for each sandbox in-memory and enforcing
#       a hard cap declared at start time. batch_start sums child caps and
#       atomically reserves them up front.
# NOT OWNS: actual wallet ledger writes. This is the v0 soft-cap layer
#           — the wallet-backed version is the next-PR follow-up that
#           pairs with the caller_api_keys table tracked next to bug #5.
# INVARIANTS:
#   * Hard cap exceedance refuses the action; partial spend isn't carved.
#   * Cap is per-sandbox; multiple parallel sandboxes don't share state.
#   * batch_start reservation is two-phase: validate-all then commit-all.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from core.sandbox.models import SandboxInvalidInput, SandboxQuotaExceeded

_LOG = logging.getLogger("aztea.sandbox.spending")

DEFAULT_SANDBOX_CAP_CENTS = 5000  # $50 default per sandbox
HARD_SANDBOX_CAP_CENTS = 50_000   # $500 absolute ceiling
DEFAULT_BATCH_CAP_CENTS = 20_000  # $200 across a full batch_start matrix

# sandbox_id → {"cap_cents": int, "spent_cents": int, "actions": int}
_BUDGETS: dict[str, dict[str, int]] = {}
_BUDGETS_LOCK = threading.RLock()


def register_cap(sandbox_id: str, cap_cents: int | None) -> int:
    """Side-effect: install a spending cap for this sandbox; returns the resolved cap.

    Why: called once at sandbox_start. Defaults to DEFAULT_SANDBOX_CAP_CENTS
    when the caller doesn't specify; clamps to HARD_SANDBOX_CAP_CENTS as a
    safety stop even if the caller asks for more.
    """
    if cap_cents is None:
        resolved = DEFAULT_SANDBOX_CAP_CENTS
    else:
        try:
            resolved = int(cap_cents)
        except (TypeError, ValueError) as exc:
            raise SandboxInvalidInput(
                "spending_cap_cents must be an integer"
            ) from exc
        if resolved <= 0:
            raise SandboxInvalidInput("spending_cap_cents must be > 0")
        resolved = min(resolved, HARD_SANDBOX_CAP_CENTS)
    with _BUDGETS_LOCK:
        _BUDGETS[sandbox_id] = {
            "cap_cents": resolved,
            "spent_cents": 0,
            "actions": 0,
        }
    return resolved


def charge(sandbox_id: str, cents: int, *, action: str = "unknown") -> dict[str, Any]:
    """Record cost for a per-sandbox action; raise when the cap would be exceeded.

    Returns the post-charge budget snapshot. The caller (the engine
    dispatcher) is responsible for calling this on actions that have a
    real cost — exec, browser, lighthouse, network_capture, etc. For
    the v0 surface we keep the cost model conservative: only
    explicitly-priced verbs charge, the rest are free.
    """
    if cents <= 0:
        return snapshot(sandbox_id)
    with _BUDGETS_LOCK:
        budget = _BUDGETS.get(sandbox_id)
        if budget is None:
            # Sandbox didn't go through register_cap; install a default
            # so we always have a cap to enforce.
            budget = _BUDGETS[sandbox_id] = {
                "cap_cents": DEFAULT_SANDBOX_CAP_CENTS,
                "spent_cents": 0,
                "actions": 0,
            }
        new_total = budget["spent_cents"] + cents
        if new_total > budget["cap_cents"]:
            raise SandboxQuotaExceeded(
                f"sandbox '{sandbox_id}' would exceed its cap of "
                f"{budget['cap_cents']} cents (spent {budget['spent_cents']}, "
                f"requested +{cents} for action='{action}'). Raise the cap "
                "at start time or stop the sandbox.",
                details={
                    "sandbox_id": sandbox_id,
                    "cap_cents": budget["cap_cents"],
                    "spent_cents": budget["spent_cents"],
                    "requested_cents": cents,
                    "action": action,
                },
            )
        budget["spent_cents"] = new_total
        budget["actions"] += 1
    return snapshot(sandbox_id)


def snapshot(sandbox_id: str) -> dict[str, Any]:
    """Pure: return the current budget snapshot for ``sandbox_id``."""
    with _BUDGETS_LOCK:
        budget = _BUDGETS.get(sandbox_id)
        if budget is None:
            return {
                "sandbox_id": sandbox_id,
                "cap_cents": DEFAULT_SANDBOX_CAP_CENTS,
                "spent_cents": 0,
                "remaining_cents": DEFAULT_SANDBOX_CAP_CENTS,
                "actions": 0,
            }
        return {
            "sandbox_id": sandbox_id,
            "cap_cents": budget["cap_cents"],
            "spent_cents": budget["spent_cents"],
            "remaining_cents": max(0, budget["cap_cents"] - budget["spent_cents"]),
            "actions": budget["actions"],
        }


def reserve_batch(per_cell_cap_cents: int, cells: int) -> dict[str, Any]:
    """Pre-hold the full batch budget before any cell starts.

    Why: pre-fix each cell in batch_start billed independently. If the
    user asked for 10 cells × $5 cap and the host already had $40 of
    other commitments, cells 9 and 10 would silently start and bill.
    Now we sum + check up front: a batch that would exceed the hard
    ceiling refuses before any container starts.
    """
    if cells <= 0:
        raise SandboxInvalidInput("cells must be > 0 for reserve_batch")
    total = per_cell_cap_cents * cells
    if total > DEFAULT_BATCH_CAP_CENTS:
        raise SandboxQuotaExceeded(
            f"batch reservation would total {total} cents which exceeds "
            f"the default batch ceiling of {DEFAULT_BATCH_CAP_CENTS} cents. "
            "Lower per-cell cap or split the matrix.",
            details={
                "per_cell_cap_cents": per_cell_cap_cents,
                "cells": cells,
                "total_cents": total,
                "ceiling_cents": DEFAULT_BATCH_CAP_CENTS,
            },
        )
    return {
        "per_cell_cap_cents": per_cell_cap_cents,
        "cells": cells,
        "total_reserved_cents": total,
        "note": (
            "Soft pre-hold (v0). Wallet-backed atomic hold lands in the "
            "follow-up paired with the caller_api_keys table."
        ),
    }


def evict(sandbox_id: str) -> bool:
    """Side-effect: drop the budget record when a sandbox stops."""
    with _BUDGETS_LOCK:
        return _BUDGETS.pop(sandbox_id, None) is not None
