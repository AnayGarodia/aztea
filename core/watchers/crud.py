"""Watcher row persistence — insert / get / list / update / delete + spend-day rollover."""

from __future__ import annotations

import json
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from core import db as _db

from .models import (
    MAX_CONSECUTIVE_ERRORS_BEFORE_PAUSE,
    STATUS_ACTIVE,
    STATUS_BUDGET_EXHAUSTED,
    STATUS_PAUSED,
    WATCHER_STATUSES,
)

DB_PATH = _db.DB_PATH


def _resolved_db_path() -> str:
    """Honor monkey-patched DB_PATH on the parent package (isolated tests)."""
    pkg = sys.modules.get("core.watchers")
    if pkg is not None:
        candidate = getattr(pkg, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return DB_PATH


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(_resolved_db_path())


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_dt().isoformat()


def _iso_after_seconds(seconds: int) -> str:
    return (_now_dt() + timedelta(seconds=seconds)).isoformat()


def _today_utc_date() -> str:
    return _now_dt().strftime("%Y-%m-%d")


def _row_to_dict(row: Any) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return dict(row)


def create_watcher(
    *,
    owner_user_id: str,
    caller_wallet_id: str,
    agent_id: str,
    target_kind: str,
    target_url: str,
    target_meta: dict | None,
    on_change_policy: str,
    tick_interval_seconds: int,
    budget_per_day_cents: int,
    delivery_webhook_url: str | None,
    delivery_email: str | None,
    payload: dict | None,
) -> dict:
    """Insert a new watcher row and return its full dict.

    The ``next_check_at`` is set to ``now`` so the sweeper picks the watcher
    up on the next pass — first fingerprint observation runs immediately.
    """
    watcher_id = f"wtch_{uuid.uuid4().hex[:24]}"
    delivery_secret = (
        secrets.token_urlsafe(32) if delivery_webhook_url else None
    )
    now = _now_iso()
    target_meta_json = json.dumps(target_meta or {}, sort_keys=True)
    payload_json = json.dumps(payload or {}, sort_keys=True)
    spend_window_date = _today_utc_date()

    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO watchers (
                watcher_id, owner_user_id, caller_wallet_id, agent_id,
                target_kind, target_url, target_meta_json,
                on_change_policy, tick_interval_seconds,
                budget_per_day_cents, spend_today_cents, spend_window_date,
                delivery_webhook_url, delivery_email, delivery_secret,
                payload_json,
                status, consecutive_errors,
                last_fingerprint, last_fingerprint_at, last_fired_job_id, last_error,
                next_check_at, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s
            )
            """,
            (
                watcher_id, owner_user_id, caller_wallet_id, agent_id,
                target_kind, target_url, target_meta_json,
                on_change_policy, int(tick_interval_seconds),
                int(budget_per_day_cents), 0, spend_window_date,
                delivery_webhook_url, delivery_email, delivery_secret,
                payload_json,
                STATUS_ACTIVE, 0,
                None, None, None, None,
                now, now, now,
            ),
        )
    fetched = get_watcher(watcher_id)
    if fetched is None:
        # Should never happen — INSERT just succeeded — but keep an explicit
        # branch so a future schema change can't silently return None.
        raise RuntimeError(f"watcher {watcher_id} not found after insert")
    return fetched


def get_watcher(watcher_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM watchers WHERE watcher_id = %s",
            (watcher_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_watchers_for_owner(owner_user_id: str, *, limit: int = 100) -> list[dict]:
    bounded_limit = max(1, min(int(limit), 500))
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM watchers
            WHERE owner_user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (owner_user_id, bounded_limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_watchers_needing_rollover(today_utc: str, *, limit: int = 200) -> list[dict]:
    """Watchers whose spend_window_date is older than today_utc, regardless
    of status. Used by the sweeper's rollover phase to flip
    ``budget_exhausted`` rows back to ``active`` after UTC midnight.

    Without this, a budget_exhausted watcher is never picked up by
    list_due_watchers (which filters status='active') and would stay
    stuck across UTC rollover indefinitely.
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM watchers
            WHERE spend_window_date < %s
            ORDER BY spend_window_date ASC
            LIMIT %s
            """,
            (today_utc, max(1, min(int(limit), 1000))),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_due_watchers(now_iso: str, *, limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM watchers
            WHERE status = %s AND next_check_at <= %s
            ORDER BY next_check_at ASC
            LIMIT %s
            """,
            (STATUS_ACTIVE, now_iso, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_unfired_runs(*, limit: int = 100) -> list[dict]:
    """Return watcher_runs rows where a job was fired but delivery hasn't completed.

    A run is "delivered" when ``finished_at`` is non-null. Used by the
    sweeper to drive watcher.fired delivery once the underlying job
    settles.
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT r.*, w.delivery_webhook_url, w.delivery_email,
                   w.delivery_secret, w.target_url, w.target_kind, w.owner_user_id
            FROM watcher_runs r
            JOIN watchers w ON w.watcher_id = r.watcher_id
            WHERE r.fired_job_id IS NOT NULL AND r.finished_at IS NULL
            ORDER BY r.started_at ASC
            LIMIT %s
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def claim_watcher_tick(watcher_id: str, prev_next_check_at: str, new_next_check_at: str) -> bool:
    """Atomically advance ``next_check_at``. Returns True iff this caller acquired the tick.

    The compare-and-swap on prev_next_check_at means two sweeper passes
    racing on the same watcher (e.g. two leader candidates briefly
    overlapping) cannot both fire it.
    """
    with _conn() as conn:
        cursor = conn.execute(
            """
            UPDATE watchers
            SET next_check_at = %s, updated_at = %s
            WHERE watcher_id = %s AND next_check_at = %s AND status = %s
            """,
            (new_next_check_at, _now_iso(), watcher_id, prev_next_check_at, STATUS_ACTIVE),
        )
    return int(getattr(cursor, "rowcount", 0) or 0) == 1


def update_status(watcher_id: str, status: str, last_error: str | None = None) -> None:
    if status not in WATCHER_STATUSES:
        raise ValueError(f"invalid watcher status: {status}")
    with _conn() as conn:
        conn.execute(
            """
            UPDATE watchers
            SET status = %s, last_error = %s, updated_at = %s
            WHERE watcher_id = %s
            """,
            (status, last_error, _now_iso(), watcher_id),
        )


def record_fire_atomic(
    watcher_id: str,
    *,
    fingerprint: str,
    fired_job_id: str,
    spend_increment_cents: int,
    spend_window_date: str,
) -> str:
    """Atomic write: spend bump + fingerprint + watcher_runs row in one
    transaction. Returns the new run_id.

    Splitting this into two `_conn()` blocks (the previous shape) means a
    crash between the run insert and the spend update leaves an
    inconsistent row pair: a fire is recorded but the budget tracker
    didn't move, and the next sweep would re-fire on the same change. The
    single transaction here guarantees either both rows or neither.
    """
    run_id = f"wrun_{uuid.uuid4().hex[:24]}"
    now = _now_iso()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO watcher_runs (
                run_id, watcher_id, started_at, finished_at,
                fingerprint, fingerprint_changed, fired_job_id, skip_reason, error
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                watcher_id,
                now,
                None,  # finished_at populated when delivery completes
                fingerprint,
                1,
                fired_job_id,
                None,
                None,
            ),
        )
        conn.execute(
            """
            UPDATE watchers
            SET last_fingerprint = %s,
                last_fingerprint_at = %s,
                last_fired_job_id = %s,
                spend_today_cents = spend_today_cents + %s,
                spend_window_date = %s,
                consecutive_errors = 0,
                last_error = NULL,
                updated_at = %s
            WHERE watcher_id = %s
            """,
            (
                fingerprint,
                now,
                fired_job_id,
                int(spend_increment_cents),
                spend_window_date,
                now,
                watcher_id,
            ),
        )
    return run_id


def record_spend_and_fingerprint(
    watcher_id: str,
    *,
    fingerprint: str,
    fired_job_id: str,
    spend_increment_cents: int,
    spend_window_date: str,
) -> None:
    """Deprecated: kept only for tests that monkeypatch this function. New
    code must use ``record_fire_atomic`` so the spend bump and run row are
    written in the same transaction."""
    with _conn() as conn:
        conn.execute(
            """
            UPDATE watchers
            SET last_fingerprint = %s,
                last_fingerprint_at = %s,
                last_fired_job_id = %s,
                spend_today_cents = spend_today_cents + %s,
                spend_window_date = %s,
                consecutive_errors = 0,
                last_error = NULL,
                updated_at = %s
            WHERE watcher_id = %s
            """,
            (
                fingerprint,
                _now_iso(),
                fired_job_id,
                int(spend_increment_cents),
                spend_window_date,
                _now_iso(),
                watcher_id,
            ),
        )


def reset_spend_window(watcher_id: str, today_utc: str) -> None:
    """Reset daily spend counters and clear ``budget_exhausted`` if set."""
    with _conn() as conn:
        conn.execute(
            """
            UPDATE watchers
            SET spend_today_cents = 0,
                spend_window_date = %s,
                status = CASE WHEN status = %s THEN %s ELSE status END,
                updated_at = %s
            WHERE watcher_id = %s
            """,
            (today_utc, STATUS_BUDGET_EXHAUSTED, STATUS_ACTIVE, _now_iso(), watcher_id),
        )


def clear_consecutive_errors(watcher_id: str) -> None:
    """Reset the consecutive-error counter on a successful fingerprint
    observation, regardless of whether the tick fired. Without this, a
    flapping target (4 errors, 1 success-no-change, 4 errors, ...) still
    auto-pauses despite reaching the target N times. Idempotent: a no-op
    when consecutive_errors is already 0."""
    with _conn() as conn:
        conn.execute(
            """
            UPDATE watchers
            SET consecutive_errors = 0,
                last_error = NULL,
                updated_at = %s
            WHERE watcher_id = %s AND consecutive_errors > 0
            """,
            (_now_iso(), watcher_id),
        )


def record_fingerprint_error(watcher_id: str, error: str) -> int:
    """Increment consecutive_errors and return the new count.

    The count drives auto-pause once it crosses MAX_CONSECUTIVE_ERRORS_BEFORE_PAUSE.
    """
    with _conn() as conn:
        conn.execute(
            """
            UPDATE watchers
            SET consecutive_errors = consecutive_errors + 1,
                last_error = %s,
                updated_at = %s
            WHERE watcher_id = %s
            """,
            (error[:500], _now_iso(), watcher_id),
        )
        row = conn.execute(
            "SELECT consecutive_errors FROM watchers WHERE watcher_id = %s",
            (watcher_id,),
        ).fetchone()
    return int(_row_to_dict(row).get("consecutive_errors") or 0)


def insert_watcher_run(
    *,
    watcher_id: str,
    fingerprint: str | None,
    fingerprint_changed: bool,
    fired_job_id: str | None,
    skip_reason: str | None,
    error: str | None,
) -> str:
    run_id = f"wrun_{uuid.uuid4().hex[:24]}"
    finished_at = None if fired_job_id else _now_iso()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO watcher_runs (
                run_id, watcher_id, started_at, finished_at,
                fingerprint, fingerprint_changed, fired_job_id, skip_reason, error
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                watcher_id,
                _now_iso(),
                finished_at,
                fingerprint,
                1 if fingerprint_changed else 0,
                fired_job_id,
                skip_reason,
                error,
            ),
        )
    return run_id


def mark_run_delivered(run_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE watcher_runs SET finished_at = %s WHERE run_id = %s AND finished_at IS NULL",
            (_now_iso(), run_id),
        )


def list_watcher_runs(watcher_id: str, *, limit: int = 50) -> list[dict]:
    bounded_limit = max(1, min(int(limit), 500))
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM watcher_runs
            WHERE watcher_id = %s
            ORDER BY started_at DESC
            LIMIT %s
            """,
            (watcher_id, bounded_limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_watcher(watcher_id: str, **fields: Any) -> dict | None:
    """Apply a partial update. Only known columns are accepted."""
    allowed = {
        "status",
        "tick_interval_seconds",
        "budget_per_day_cents",
        "delivery_webhook_url",
        "delivery_email",
        "on_change_policy",
    }
    set_clauses: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "status" and value not in (STATUS_ACTIVE, STATUS_PAUSED):
            raise ValueError("status may only be set to 'active' or 'paused'.")
        set_clauses.append(f"{key} = %s")
        values.append(value)
    if not set_clauses:
        return get_watcher(watcher_id)
    # Reactivating a watcher (active after a pause/budget_exhausted) should
    # clear last_error and the consecutive-error counter. Otherwise the
    # stale error message and counter persist into the next pass and a
    # single new error re-trips the auto-pause threshold immediately.
    if fields.get("status") == STATUS_ACTIVE:
        set_clauses.append("last_error = NULL")
        set_clauses.append("consecutive_errors = 0")
    set_clauses.append("updated_at = %s")
    values.append(_now_iso())
    values.append(watcher_id)
    with _conn() as conn:
        conn.execute(
            f"UPDATE watchers SET {', '.join(set_clauses)} WHERE watcher_id = %s",
            tuple(values),
        )
    return get_watcher(watcher_id)


def delete_watcher(watcher_id: str) -> bool:
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM watcher_runs WHERE watcher_id = %s", (watcher_id,)
        )
        cursor = conn.execute(
            "DELETE FROM watchers WHERE watcher_id = %s", (watcher_id,)
        )
        deleted = int(getattr(cursor, "rowcount", 0) or 0) >= 1
    return deleted


__all__ = [
    "MAX_CONSECUTIVE_ERRORS_BEFORE_PAUSE",
    "claim_watcher_tick",
    "create_watcher",
    "delete_watcher",
    "get_watcher",
    "insert_watcher_run",
    "list_due_watchers",
    "list_unfired_runs",
    "list_watcher_runs",
    "list_watchers_for_owner",
    "mark_run_delivered",
    "record_fingerprint_error",
    "record_spend_and_fingerprint",
    "reset_spend_window",
    "update_status",
    "update_watcher",
]
