"""
queries.py — shared read helpers for the Otto telemetry dashboard.

All dashboard sections pull through these so the SQLite/Postgres portability
rules live in one place: dict rows from db.py, app-side percentiles (SQLite has
no percentile_cont), and a small window/day-range helper that works off the
pre-computed `day` column instead of backend date functions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core import db as _db

# Allowed dashboard windows → number of days. The dashboard sends one of these;
# anything else is a client bug and should 400 (callers validate against this).
ALLOWED_WINDOWS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_WINDOW = "30d"


def now() -> datetime:
    return datetime.now(timezone.utc)


def window_days(window: str) -> int:
    return ALLOWED_WINDOWS.get(window, ALLOWED_WINDOWS[DEFAULT_WINDOW])


def day_bounds(window: str) -> tuple[str, str, str]:
    """Return (start_day, prior_start_day, today) as YYYY-MM-DD strings.

    `prior_start_day` begins a same-sized window immediately before the current
    one, so every headline number can be reported with a delta vs the prior
    period.
    """
    span = window_days(window)
    today = now()
    start = today - timedelta(days=span - 1)
    prior_start = start - timedelta(days=span)
    fmt = "%Y-%m-%d"
    return start.strftime(fmt), prior_start.strftime(fmt), today.strftime(fmt)


def day_series(window: str) -> list[str]:
    """Every day (YYYY-MM-DD) in the window, oldest first — so a timeseries can
    be zero-filled for days with no events (gaps read as outages otherwise)."""
    span = window_days(window)
    today = now()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span)]
    return list(reversed(days))


def fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn: _db.DbConnection = _db.get_raw_connection(_db.DB_PATH)
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def fetchone(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = fetchall(sql, params)
    return rows[0] if rows else None


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (pct 0-100). None on empty input.

    SQLite ships without percentile_cont and we won't pull numpy for one number;
    nearest-rank is fine for a latency dashboard.
    """
    if not values:
        return None
    if pct <= 0:
        return float(min(values))
    if pct >= 100:
        return float(max(values))
    ordered = sorted(float(v) for v in values)
    idx = int(round(pct / 100.0 * len(ordered) + 0.5)) - 1
    idx = max(0, min(idx, len(ordered) - 1))
    return float(ordered[idx])


def pct_delta(current: float, prior: float) -> float | None:
    """Percentage change prior→current. None when there's no prior baseline."""
    if prior == 0:
        return None
    return round((current - prior) / prior * 100.0, 1)


def trend(value: float, prior: float) -> dict[str, Any]:
    """A headline number plus its prior-period baseline and delta — the shape
    every KPI card on the dashboard renders."""
    return {"value": value, "prior": prior, "delta_pct": pct_delta(value, prior)}


def rate(numerator: float, denominator: float, digits: int = 3) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, digits)


def task_latency_values(start_day: str, column: str, app: str | None = None,
                        intent: str | None = None) -> list[float]:
    """Pull a latency column's raw values for percentile math. `column` is a
    fixed identifier from the caller (never user input), so interpolating it is
    safe and lets one helper serve every latency metric."""
    sql = (
        f"SELECT {column} AS v FROM otto_telemetry_events "
        "WHERE event='task' AND day >= %s AND {col} IS NOT NULL"
    ).format(col=column)
    params: list[Any] = [start_day]
    if app:
        sql += " AND app = %s"
        params.append(app)
    if intent:
        sql += " AND intent_category = %s"
        params.append(intent)
    return [float(r["v"]) for r in fetchall(sql, tuple(params)) if r.get("v") is not None]
