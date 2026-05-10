"""Watcher sweeper — periodic fingerprint + diff-gated job firing.

# OWNS: per-tick fingerprint compare, budget rollover/gate, job firing,
#       deferred delivery of completed runs.
# NOT OWNS: HTTP routes (server/application_parts), wallet ledger
#           (core.payments), webhook delivery (core.watchers.delivery).
# INVARIANTS:
# - A tick that does not observe a fingerprint change MUST NOT charge.
# - Fingerprint errors (not changes) NEVER trigger a fire.
# - Wallet charge happens ONLY after the diff gate passes AND the budget
#   gate passes — both gates are checked under the same Python call so
#   they cannot drift out of sync with `last_fingerprint` and `spend_today_cents`.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from core import jobs
from core import payments

from . import crud as _crud
from . import delivery as _delivery
from .fingerprint import fingerprint_target
from .models import (
    MAX_CONSECUTIVE_ERRORS_BEFORE_PAUSE,
    STATUS_ACTIVE,
    STATUS_BUDGET_EXHAUSTED,
    STATUS_PAUSED,
)

_LOG = logging.getLogger(__name__)

WATCHER_TICK_SECONDS = max(
    5, int(os.environ.get("AZTEA_WATCHER_TICK_SECONDS", "30") or "30")
)
WATCHER_BATCH_LIMIT = max(
    1, int(os.environ.get("AZTEA_WATCHER_BATCH_LIMIT", "50") or "50")
)
WATCHERS_ENABLED = (
    os.environ.get("AZTEA_WATCHERS_ENABLED", "1").strip().lower()
    not in ("0", "false", "no")
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _iso_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat()


# ---------------------------------------------------------------------------
# Public sweeper entry point
# ---------------------------------------------------------------------------


def sweep_watchers(*, limit: int = WATCHER_BATCH_LIMIT) -> dict[str, int]:
    """One sweeper pass: rollover spend windows, deliver pending runs, then
    process due watchers.

    Returns a small summary dict for sweeper-state telemetry.
    """
    delivered = 0
    fired = 0
    skipped_no_change = 0
    skipped_budget = 0
    errored = 0
    rolled_over = 0

    # Phase 0: roll over spend windows for any watcher whose window date is
    # behind today_utc, regardless of status. This MUST run before
    # list_due_watchers because that query filters status='active' and
    # would otherwise leave budget_exhausted rows stuck across UTC midnight.
    try:
        rolled_over = _rollover_spend_windows(limit=limit * 4)
    except Exception:
        _LOG.exception("Watcher rollover phase failed.")

    # Phase 1: drive delivery for runs whose fired job has settled.
    try:
        delivered = _deliver_pending_runs(limit=limit)
    except Exception:
        _LOG.exception("Watcher delivery phase failed.")

    # Phase 2: process due watchers.
    try:
        due = _crud.list_due_watchers(_now_iso(), limit=limit)
    except Exception:
        _LOG.exception("Failed to list due watchers.")
        due = []

    for row in due:
        try:
            outcome = _process_due_watcher(row)
        except Exception:
            _LOG.exception(
                "Unhandled exception processing watcher %s",
                row.get("watcher_id"),
            )
            errored += 1
            continue
        if outcome == "fired":
            fired += 1
        elif outcome == "no_change":
            skipped_no_change += 1
        elif outcome == "budget_exhausted":
            skipped_budget += 1
        elif outcome == "error":
            errored += 1

    return {
        "delivered": delivered,
        "fired": fired,
        "skipped_no_change": skipped_no_change,
        "skipped_budget_exhausted": skipped_budget,
        "errored": errored,
        "rolled_over": rolled_over,
    }


def _rollover_spend_windows(*, limit: int) -> int:
    """Reset spend_today + status=active for every watcher whose
    spend_window_date is behind today_utc. Returns the row count rolled.

    This runs as a separate sweep phase rather than inline in
    _process_due_watcher because the list_due_watchers query filters
    status='active' — so a budget_exhausted watcher would never reach
    the inline rollover branch.
    """
    today = _today_utc()
    rolled = 0
    for row in _crud.list_watchers_needing_rollover(today, limit=limit):
        _crud.reset_spend_window(row["watcher_id"], today)
        rolled += 1
    return rolled


def watchers_sweeper_loop(stop_event: threading.Event) -> None:
    """Daemon-thread loop. Wakes every WATCHER_TICK_SECONDS until stopped."""
    _LOG.info("Watcher sweeper started (tick=%ss).", WATCHER_TICK_SECONDS)
    while not stop_event.wait(WATCHER_TICK_SECONDS):
        try:
            sweep_watchers(limit=WATCHER_BATCH_LIMIT)
        except Exception:
            _LOG.exception("Watcher sweeper tick failed.")
    _LOG.info("Watcher sweeper stopped.")


# ---------------------------------------------------------------------------
# Per-watcher tick processing
# ---------------------------------------------------------------------------


def _process_due_watcher(row: dict) -> str:
    """Process a single due watcher.

    Returns one of: 'fired', 'no_change', 'budget_exhausted', 'paused',
    'claim_lost', 'error'.
    """
    watcher_id = row["watcher_id"]
    prev_next_check_at = row["next_check_at"]
    new_next_check_at = _iso_after(int(row["tick_interval_seconds"]))
    if not _crud.claim_watcher_tick(watcher_id, prev_next_check_at, new_next_check_at):
        return "claim_lost"

    # Roll daily spend window if we crossed UTC midnight.
    today = _today_utc()
    if row.get("spend_window_date") != today:
        _crud.reset_spend_window(watcher_id, today)
        row["spend_today_cents"] = 0
        row["spend_window_date"] = today
        if row.get("status") == STATUS_BUDGET_EXHAUSTED:
            row["status"] = STATUS_ACTIVE

    fingerprint, error = fingerprint_target(
        row["target_kind"],
        row["target_url"],
        _safe_json_load(row.get("target_meta_json"), {}),
    )

    if error is not None:
        new_count = _crud.record_fingerprint_error(watcher_id, error)
        if new_count >= MAX_CONSECUTIVE_ERRORS_BEFORE_PAUSE:
            _crud.update_status(
                watcher_id,
                STATUS_PAUSED,
                last_error=f"auto-paused after {new_count} consecutive errors: {error}",
            )
        _crud.insert_watcher_run(
            watcher_id=watcher_id,
            fingerprint=None,
            fingerprint_changed=False,
            fired_job_id=None,
            skip_reason="target_error",
            error=error,
        )
        return "error"

    # Successful fingerprint observation — reset the error counter even
    # if we're about to skip the diff gate. A flapping target that reaches
    # us 1-in-5 ticks should NOT auto-pause.
    _crud.clear_consecutive_errors(watcher_id)

    # Diff gate.
    policy = row.get("on_change_policy") or "on_change"
    fingerprint_changed = (
        fingerprint != (row.get("last_fingerprint") or None)
    )
    if policy == "on_change" and not fingerprint_changed:
        _crud.insert_watcher_run(
            watcher_id=watcher_id,
            fingerprint=fingerprint,
            fingerprint_changed=False,
            fired_job_id=None,
            skip_reason="no_change",
            error=None,
        )
        return "no_change"

    # Budget gate.
    estimate = _estimate_price_cents(row["agent_id"])
    if estimate is None:
        _crud.insert_watcher_run(
            watcher_id=watcher_id,
            fingerprint=fingerprint,
            fingerprint_changed=fingerprint_changed,
            fired_job_id=None,
            skip_reason="agent_missing",
            error="agent not found or unpriced",
        )
        return "error"

    spend_today = int(row.get("spend_today_cents") or 0)
    budget = int(row.get("budget_per_day_cents") or 0)
    if spend_today + estimate > budget:
        _crud.update_status(
            watcher_id,
            STATUS_BUDGET_EXHAUSTED,
            last_error=(
                f"budget_exhausted: spend_today={spend_today}c "
                f"+ next={estimate}c > budget={budget}c"
            ),
        )
        _crud.insert_watcher_run(
            watcher_id=watcher_id,
            fingerprint=fingerprint,
            fingerprint_changed=fingerprint_changed,
            fired_job_id=None,
            skip_reason="budget_exhausted",
            error=None,
        )
        return "budget_exhausted"

    # Fire.
    fired = _fire(row, fingerprint=fingerprint, price_cents=estimate)
    if fired is None:
        return "error"
    job_id, run_id = fired
    _ = run_id  # delivery picks up by querying watcher_runs
    return "fired"


def _fire(
    row: dict,
    *,
    fingerprint: str,
    price_cents: int,
) -> tuple[str, str] | None:
    """Charge + create job. Returns (job_id, run_id) on success.

    On any failure after pre_call_charge, refund and return None.
    """
    watcher_id = row["watcher_id"]
    agent_id = row["agent_id"]
    caller_owner_id = row["owner_user_id"]
    payload = _safe_json_load(row.get("payload_json"), {})

    agent = _get_agent_or_none(agent_id)
    if agent is None:
        _crud.insert_watcher_run(
            watcher_id=watcher_id,
            fingerprint=fingerprint,
            fingerprint_changed=True,
            fired_job_id=None,
            skip_reason="agent_missing",
            error="agent not found at fire time",
        )
        return None

    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    fee_bearer_policy = payments.normalize_fee_bearer_policy("caller")
    distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=int(payments.PLATFORM_FEE_PCT),
        fee_bearer_policy=fee_bearer_policy,
    )
    caller_charge_cents = int(distribution["caller_charge_cents"])

    try:
        charge_tx_id = payments.pre_call_charge(
            caller_wallet["wallet_id"],
            caller_charge_cents,
            agent_id,
        )
    except payments.InsufficientBalanceError:
        _crud.update_status(
            watcher_id,
            STATUS_BUDGET_EXHAUSTED,
            last_error="wallet underfunded at fire time",
        )
        _crud.insert_watcher_run(
            watcher_id=watcher_id,
            fingerprint=fingerprint,
            fingerprint_changed=True,
            fired_job_id=None,
            skip_reason="insufficient_funds",
            error=None,
        )
        return None
    except Exception as exc:
        _crud.insert_watcher_run(
            watcher_id=watcher_id,
            fingerprint=fingerprint,
            fingerprint_changed=True,
            fired_job_id=None,
            skip_reason="charge_failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        return None

    # client_id includes the fingerprint prefix so a sweeper restart that
    # re-runs the same tick is naturally idempotent at the audit-trail layer:
    # two jobs with the same client_id are visibly the result of a duplicated
    # sweeper pass, not independent fires.
    client_id = f"watcher:{watcher_id}:{fingerprint[:12]}"
    try:
        job = jobs.create_job(
            agent_id=agent_id,
            caller_owner_id=caller_owner_id,
            caller_wallet_id=caller_wallet["wallet_id"],
            agent_wallet_id=agent_wallet["wallet_id"],
            platform_wallet_id=platform_wallet["wallet_id"],
            price_cents=price_cents,
            caller_charge_cents=caller_charge_cents,
            platform_fee_pct_at_create=int(payments.PLATFORM_FEE_PCT),
            fee_bearer_policy=fee_bearer_policy,
            client_id=client_id,
            charge_tx_id=charge_tx_id,
            input_payload=payload,
            agent_owner_id=agent.get("owner_id"),
        )
    except Exception as exc:
        try:
            payments.post_call_refund(
                caller_wallet["wallet_id"],
                charge_tx_id,
                caller_charge_cents,
                agent_id,
            )
        except Exception:
            _LOG.exception(
                "Refund after failed create_job for watcher %s failed.",
                watcher_id,
            )
        _crud.insert_watcher_run(
            watcher_id=watcher_id,
            fingerprint=fingerprint,
            fingerprint_changed=True,
            fired_job_id=None,
            skip_reason="job_create_failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        return None

    job_id = job["job_id"]
    # Atomic: insert watcher_runs row + bump spend in one transaction so a
    # crash between them cannot produce inconsistent state.
    run_id = _crud.record_fire_atomic(
        watcher_id,
        fingerprint=fingerprint,
        fired_job_id=job_id,
        spend_increment_cents=caller_charge_cents,
        spend_window_date=_today_utc(),
    )
    return job_id, run_id


# ---------------------------------------------------------------------------
# Delivery phase — drive completed runs to webhook + email
# ---------------------------------------------------------------------------


def _deliver_pending_runs(*, limit: int = WATCHER_BATCH_LIMIT) -> int:
    """Mark each fired run delivered once its job reaches a terminal status."""
    delivered = 0
    rows = _crud.list_unfired_runs(limit=limit)
    for run in rows:
        job_id = run.get("fired_job_id")
        if not job_id:
            continue
        job = jobs.get_job(job_id)
        if job is None:
            # Job deleted (rare); skip — sweeper will re-check on next pass.
            continue
        status = (job.get("status") or "").strip().lower()
        if status not in ("complete", "failed", "cancelled", "canceled"):
            continue
        try:
            _delivery.deliver_run(run, job)
        except Exception:
            _LOG.exception(
                "Delivery for watcher_run %s failed; will retry next tick.",
                run.get("run_id"),
            )
            continue
        _crud.mark_run_delivered(run["run_id"])
        delivered += 1
    return delivered


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json_load(blob: str | None, default: Any) -> Any:
    if not blob:
        return default
    try:
        import json
        return json.loads(blob)
    except (TypeError, ValueError):
        return default


def _get_agent_or_none(agent_id: str) -> dict | None:
    try:
        from core import registry
        return registry.get_agent(agent_id, include_unapproved=True)
    except Exception:
        _LOG.exception("registry.get_agent failed for %s", agent_id)
        return None


def _estimate_price_cents(agent_id: str) -> int | None:
    agent = _get_agent_or_none(agent_id)
    if agent is None:
        return None
    raw = agent.get("price_per_call_cents")
    if raw is None:
        usd = agent.get("price_per_call_usd")
        if usd is None:
            return None
        try:
            return int(round(float(usd) * 100))
        except (TypeError, ValueError):
            return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


__all__ = [
    "WATCHER_TICK_SECONDS",
    "WATCHER_BATCH_LIMIT",
    "WATCHERS_ENABLED",
    "sweep_watchers",
    "watchers_sweeper_loop",
]
