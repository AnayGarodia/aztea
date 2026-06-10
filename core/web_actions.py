"""Durable state for escrowed web actions — the fail-FORWARD reconcile foundation (E2).

*** NOT WIRED in this PR: no production path writes a web_actions row yet (web_actor.
_commit returns execution='deferred'). This is the tested state-machine + reconcile
foundation the deferred money-PR builds on — it moves no money on its own. ***

# OWNS: the web_actions row lifecycle (phase + commit_phase) and the pure
#        fail-forward reconcile decision. The durable commit_phase is what lets a
#        sweeper recover a worker that died mid-action WITHOUT double-charging the
#        caller or wrongly refunding a completed real-world action.
# NOT OWNS: the ledger money movement (the focused E2 money-PR — it needs new
#           guarded entries with the action_escrow split, not the 90/10 payout
#           split, so it is deliberately separate), the mandate lifecycle
#           (core.action_mandates), or the browser action (agents.web_actor).
# INVARIANTS:
#   * commit_phase only advances pre_submit -> submitted -> settled, never back.
#   * FAIL-FORWARD: an action that reached 'submitted' (the irreversible web action
#     happened) may only be SETTLED, never refunded — the caller already paid the
#     merchant. Only 'pre_submit' (nothing irreversible yet) may be refunded. This is
#     enforced at the SQL level: each terminal transition pins the source commit_phase.
#   * Every transition is rowcount-guarded so a racing sweeper + worker can't both act.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from core import db as _db

DB_PATH = _db.DB_PATH
_local = _db._local  # so tests/integration helpers can close this module's connection
_LOG = logging.getLogger(__name__)

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# commit_phase values (mirror migration 0084's CHECK constraint).
PRE_SUBMIT = "pre_submit"
SUBMITTED = "submitted"
SETTLED = "settled"

# reconcile outcomes (what a sweeper must do with an abandoned action).
RECONCILE_REFUND = "refund"
RECONCILE_SETTLE_FORWARD = "settle_forward"
RECONCILE_SKIP = "skip"


def reconcile_action(commit_phase: str) -> str:
    """Pure: the FAIL-FORWARD decision for an abandoned (stale, unsettled) action.

    pre_submit -> refund         (nothing irreversible happened; return the hold)
    submitted  -> settle_forward (the merchant action DID happen; the caller already
                                  paid, so settle — never refund a completed action)
    settled / anything else -> skip (already resolved)
    """
    if commit_phase == PRE_SUBMIT:
        return RECONCILE_REFUND
    if commit_phase == SUBMITTED:
        return RECONCILE_SETTLE_FORWARD
    return RECONCILE_SKIP


def _new_id() -> str:
    n = int.from_bytes(secrets.token_bytes(16), "big")
    chars: list[str] = []
    while n:
        n, rem = divmod(n, 62)
        chars.append(_BASE62[rem])
    return "wact_" + "".join(reversed(chars)).rjust(22, "0")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(DB_PATH)


def init_web_actions_db() -> None:
    """Ensure the web_actions table exists via migrations (single schema source)."""
    from core.migrate import apply_migrations

    apply_migrations(DB_PATH)


def create_web_action(
    *, mandate_id: str, job_id: str | None = None, target_domain: str | None = None,
    quoted_cost_cents: int = 0, agent_fee_cents: int = 0,
) -> dict[str, Any]:
    """Insert a new action in phase='executing', commit_phase='pre_submit'.

    pre_submit means: the escrow hold may exist, but nothing irreversible has run, so
    this row is still safe to refund.
    """
    action_id = _new_id()
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO web_actions (action_id, job_id, mandate_id, phase, commit_phase,
                target_domain, quoted_cost_cents, agent_fee_cents, created_at, updated_at)
            VALUES (%s, %s, %s, 'executing', 'pre_submit', %s, %s, %s, %s, %s)
            """,
            (action_id, job_id, mandate_id, target_domain, int(quoted_cost_cents),
             int(agent_fee_cents), now, now),
        )
    return get_web_action(action_id) or {}


def get_web_action(action_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM web_actions WHERE action_id = %s", (action_id,)
        ).fetchone()


def mark_submitted(action_id: str) -> bool:
    """pre_submit -> submitted: record that the irreversible web action just happened.

    Rowcount-guarded and pinned to commit_phase='pre_submit', so it is set exactly
    once. After this returns True the action may ONLY be settled (fail-forward).
    """
    with _conn() as conn:
        result = conn.execute(
            "UPDATE web_actions SET commit_phase = 'submitted', updated_at = %s "
            "WHERE action_id = %s AND commit_phase = 'pre_submit'",
            (_now(), action_id),
        )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def settle_completed(action_id: str, *, actual_cost_cents: int, platform_fee_cents: int) -> bool:
    """submitted -> settled + phase='completed' (money paid out).

    Pinned to commit_phase='submitted': you can only settle-as-completed an action
    that actually submitted. The ledger money movement is the separate money-PR; this
    records the durable terminal state + the audit echoes.
    """
    with _conn() as conn:
        result = conn.execute(
            "UPDATE web_actions SET commit_phase = 'settled', phase = 'completed', "
            "actual_cost_cents = %s, platform_fee_cents = %s, settled_at = %s, updated_at = %s "
            "WHERE action_id = %s AND commit_phase = 'submitted'",
            (int(actual_cost_cents), int(platform_fee_cents), _now(), _now(), action_id),
        )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def settle_refunded(action_id: str, *, failure_code: str) -> bool:
    """pre_submit -> settled + phase='failed' (the hold is refunded).

    Pinned to commit_phase='pre_submit': you may ONLY refund an action that never
    submitted. A 'submitted' action can't be refunded here — the fail-forward guard.
    """
    with _conn() as conn:
        result = conn.execute(
            "UPDATE web_actions SET commit_phase = 'settled', phase = 'failed', "
            "failure_code = %s, settled_at = %s, updated_at = %s "
            "WHERE action_id = %s AND commit_phase = 'pre_submit'",
            (str(failure_code), _now(), _now(), action_id),
        )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def list_stale_unsettled(older_than_iso: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Actions still executing + unsettled that haven't moved since older_than_iso —
    the sweeper's worklist. The sweeper calls reconcile_action(commit_phase) on each."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM web_actions WHERE commit_phase != 'settled' "
            "AND phase = 'executing' AND updated_at <= %s "
            "ORDER BY updated_at ASC LIMIT %s",
            (older_than_iso, int(limit)),
        ).fetchall()
