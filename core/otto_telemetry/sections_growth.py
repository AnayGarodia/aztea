"""
sections_growth.py — Overview, Growth & Retention, Usage & Demand sections.

These answer "how many people use Otto, do they come back, and what do they ask
it to do." Everything groups on the pre-computed `day` column and counts
DISTINCT device_id for active-user math (no PII, anonymous installs).
"""

from __future__ import annotations

from typing import Any

from core.otto_telemetry import queries as q

# ── Overview (KPI cards) ────────────────────────────────────────────────────


def overview(window: str) -> dict[str, Any]:
    start, prior_start, _today = q.day_bounds(window)

    def _count(event: str, s: str, e: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM otto_telemetry_events WHERE event=%s AND day >= %s"
        params: list[Any] = [event, s]
        if e:
            sql += " AND day < %s"
            params.append(e)
        return int((q.fetchone(sql, tuple(params)) or {}).get("n") or 0)

    def _distinct_devices(s: str, e: str | None = None) -> int:
        sql = "SELECT COUNT(DISTINCT device_id) AS n FROM otto_telemetry_events WHERE day >= %s AND device_id IS NOT NULL"
        params: list[Any] = [s]
        if e:
            sql += " AND day < %s"
            params.append(e)
        return int((q.fetchone(sql, tuple(params)) or {}).get("n") or 0)

    downloads = _count("download", start)
    installs = _count("install", start)
    active = _distinct_devices(start)
    active_prior = _distinct_devices(prior_start, start)

    tasks_row = q.fetchone(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS ok, "
        "SUM(COALESCE(cost_usd,0)) AS cost "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s",
        (start,),
    ) or {}
    tasks = int(tasks_row.get("n") or 0)
    ok = int(tasks_row.get("ok") or 0)
    total_cost = float(tasks_row.get("cost") or 0.0)

    p95_total = q.percentile(q.task_latency_values(start, "total_ms"), 95)
    p95_ttfa = q.percentile(q.task_latency_values(start, "ttfa_ms"), 95)

    return {
        "downloads": q.trend(downloads, _count("download", prior_start, start)),
        "installs": q.trend(installs, _count("install", prior_start, start)),
        "active_devices": q.trend(active, active_prior),
        "tasks": tasks,
        "success_rate": q.rate(ok, tasks),
        "p95_total_ms": p95_total,
        "p95_ttfa_ms": p95_ttfa,
        "cost_per_task_usd": round(total_cost / tasks, 4) if tasks else None,
    }


# ── Growth & Retention ──────────────────────────────────────────────────────


def _active_timeseries(window: str) -> list[dict[str, Any]]:
    """Daily distinct active devices, zero-filled across the window."""
    rows = q.fetchall(
        "SELECT day, COUNT(DISTINCT device_id) AS devices, COUNT(*) AS events "
        "FROM otto_telemetry_events WHERE day >= %s AND device_id IS NOT NULL "
        "GROUP BY day",
        (q.day_bounds(window)[0],),
    )
    by_day = {r["day"]: r for r in rows}
    return [
        {
            "day": d,
            "devices": int((by_day.get(d) or {}).get("devices") or 0),
            "events": int((by_day.get(d) or {}).get("events") or 0),
        }
        for d in q.day_series(window)
    ]


def _funnel(window: str) -> dict[str, Any]:
    """download → install → first task → retained (came back a different day)."""
    start = q.day_bounds(window)[0]
    downloads = int(
        (q.fetchone("SELECT COUNT(*) AS n FROM otto_telemetry_events WHERE event='download' AND day >= %s", (start,)) or {}).get("n")
        or 0
    )
    installs = int(
        (q.fetchone("SELECT COUNT(DISTINCT device_id) AS n FROM otto_telemetry_events WHERE event='install' AND day >= %s", (start,)) or {}).get("n")
        or 0
    )
    activated = int(
        (q.fetchone(
            "SELECT COUNT(DISTINCT device_id) AS n FROM otto_telemetry_events "
            "WHERE event='task' AND outcome='success' AND day >= %s",
            (start,),
        ) or {}).get("n") or 0
    )
    # Retained = device with task events on 2+ distinct days.
    retained = int(
        (q.fetchone(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT device_id FROM otto_telemetry_events "
            "  WHERE event='task' AND day >= %s AND device_id IS NOT NULL "
            "  GROUP BY device_id HAVING COUNT(DISTINCT day) >= 2"
            ") sub",
            (start,),
        ) or {}).get("n") or 0
    )
    return {
        "downloads": downloads,
        "installs": installs,
        "activated": activated,
        "retained": retained,
    }


def _retention_cohorts(window: str) -> list[dict[str, Any]]:
    """For each install day, how many of that cohort were active 1 / 7 days later.
    Computed app-side from per-device (install_day, active_days) to stay portable."""
    start = q.day_bounds(window)[0]
    installs = q.fetchall(
        "SELECT device_id, MIN(day) AS install_day FROM otto_telemetry_events "
        "WHERE event='install' AND day >= %s AND device_id IS NOT NULL GROUP BY device_id",
        (start,),
    )
    if not installs:
        return []
    active = q.fetchall(
        "SELECT DISTINCT device_id, day FROM otto_telemetry_events "
        "WHERE day >= %s AND device_id IS NOT NULL",
        (start,),
    )
    active_by_device: dict[str, set[str]] = {}
    for r in active:
        active_by_device.setdefault(r["device_id"], set()).add(r["day"])

    from datetime import datetime, timedelta

    def _plus(day: str, n: int) -> str:
        return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")

    cohorts: dict[str, dict[str, int]] = {}
    for row in installs:
        dev, iday = row["device_id"], row["install_day"]
        c = cohorts.setdefault(iday, {"size": 0, "d1": 0, "d7": 0})
        c["size"] += 1
        days = active_by_device.get(dev, set())
        if _plus(iday, 1) in days:
            c["d1"] += 1
        if any(_plus(iday, k) in days for k in range(7, 14)):
            c["d7"] += 1
    return [
        {
            "cohort_day": day,
            "size": c["size"],
            "d1_pct": q.rate(c["d1"], c["size"], 2),
            "d7_pct": q.rate(c["d7"], c["size"], 2),
        }
        for day, c in sorted(cohorts.items())
    ]


def growth(window: str) -> dict[str, Any]:
    return {
        "active_timeseries": _active_timeseries(window),
        "funnel": _funnel(window),
        "retention_cohorts": _retention_cohorts(window),
    }


# ── Usage & Demand ──────────────────────────────────────────────────────────


def usage(window: str) -> dict[str, Any]:
    start = q.day_bounds(window)[0]
    tasks_by_day = q.fetchall(
        "SELECT day, COUNT(*) AS n FROM otto_telemetry_events WHERE event='task' AND day >= %s GROUP BY day",
        (start,),
    )
    by_day = {r["day"]: int(r["n"]) for r in tasks_by_day}
    series = [{"day": d, "tasks": by_day.get(d, 0)} for d in q.day_series(window)]

    top_intents = q.fetchall(
        "SELECT COALESCE(intent_category,'other') AS intent, COUNT(*) AS n "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s "
        "GROUP BY intent_category ORDER BY n DESC LIMIT 12",
        (start,),
    )
    top_apps = q.fetchall(
        "SELECT COALESCE(app,'(unknown)') AS app, COUNT(*) AS n "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s "
        "GROUP BY app ORDER BY n DESC LIMIT 12",
        (start,),
    )
    # Unmet demand: tasks Otto could not do (no skill / refused). The roadmap, ranked.
    unmet = q.fetchall(
        "SELECT COALESCE(intent_category,'other') AS intent, COALESCE(app,'(unknown)') AS app, COUNT(*) AS n "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s "
        "AND failure_reason IN ('model_refused','element_not_found') "
        "GROUP BY intent_category, app ORDER BY n DESC LIMIT 15",
        (start,),
    )
    summon = q.fetchone(
        "SELECT SUM(CASE WHEN summon='voice' THEN 1 ELSE 0 END) AS voice, "
        "SUM(CASE WHEN summon='typed' THEN 1 ELSE 0 END) AS typed "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s",
        (start,),
    ) or {}
    active_devices = int(
        (q.fetchone(
            "SELECT COUNT(DISTINCT device_id) AS n FROM otto_telemetry_events "
            "WHERE event='task' AND day >= %s AND device_id IS NOT NULL",
            (start,),
        ) or {}).get("n") or 0
    )
    total_tasks = sum(by_day.values())
    return {
        "tasks_timeseries": series,
        "tasks_per_active_device": round(total_tasks / active_devices, 2) if active_devices else None,
        "top_intents": top_intents,
        "top_apps": top_apps,
        "unmet_demand": unmet,
        "summon": {"voice": int(summon.get("voice") or 0), "typed": int(summon.get("typed") or 0)},
    }
