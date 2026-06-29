"""
sections_ops.py — Cost & Margin, Reliability, Setup & Onboarding, Learning.

Cost answers "does the business work" (per-task COGS vs price). Reliability
tracks app-level failures distinct from task outcomes. Setup surfaces the silent
killers (permission-grant drop-off, onboarding abandonment). Learning proves the
"gets smarter on repeats" claim.
"""

from __future__ import annotations

import json
from typing import Any

from core.otto_telemetry import queries as q

# ── Cost & Margin ───────────────────────────────────────────────────────────


def cost(window: str) -> dict[str, Any]:
    start = q.day_bounds(window)[0]
    totals = q.fetchone(
        "SELECT COUNT(*) AS n, SUM(COALESCE(cost_usd,0)) AS cost "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s",
        (start,),
    ) or {}
    n = int(totals.get("n") or 0)
    total_cost = float(totals.get("cost") or 0.0)

    by_intent = q.fetchall(
        "SELECT COALESCE(intent_category,'other') AS intent, COUNT(*) AS n, "
        "SUM(COALESCE(cost_usd,0)) AS cost FROM otto_telemetry_events "
        "WHERE event='task' AND day >= %s GROUP BY intent_category ORDER BY cost DESC LIMIT 12",
        (start,),
    )
    for r in by_intent:
        cnt = int(r["n"])
        r["cost_per_task_usd"] = round(float(r["cost"] or 0) / cnt, 4) if cnt else None

    # Heaviest devices by spend — the ones a flat price could make unprofitable.
    by_device = q.fetchall(
        "SELECT device_id, COUNT(*) AS n, SUM(COALESCE(cost_usd,0)) AS cost "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s AND device_id IS NOT NULL "
        "GROUP BY device_id ORDER BY cost DESC LIMIT 10",
        (start,),
    )
    for r in by_device:
        r["cost_usd"] = round(float(r["cost"] or 0), 4)
        r.pop("cost", None)

    cost_by_day = q.fetchall(
        "SELECT day, SUM(COALESCE(cost_usd,0)) AS cost, COUNT(*) AS n "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s GROUP BY day",
        (start,),
    )
    by_day = {r["day"]: r for r in cost_by_day}
    series = [
        {
            "day": d,
            "cost_usd": round(float((by_day.get(d) or {}).get("cost") or 0), 4),
            "tasks": int((by_day.get(d) or {}).get("n") or 0),
        }
        for d in q.day_series(window)
    ]
    return {
        "total_cost_usd": round(total_cost, 4),
        "cost_per_task_usd": round(total_cost / n, 4) if n else None,
        "cost_by_intent": by_intent,
        "top_cost_devices": by_device,
        "cost_timeseries": series,
    }


# ── Reliability ─────────────────────────────────────────────────────────────


def reliability(window: str) -> dict[str, Any]:
    start = q.day_bounds(window)[0]
    by_kind: dict[str, int] = {}
    rows = q.fetchall(
        "SELECT props FROM otto_telemetry_events WHERE event='error' AND day >= %s LIMIT 10000",
        (start,),
    )
    for r in rows:
        try:
            kind = (json.loads(r.get("props") or "{}") or {}).get("kind") or "unknown"
        except (TypeError, ValueError):
            kind = "unknown"
        by_kind[str(kind)] = by_kind.get(str(kind), 0) + 1

    active_devices = int(
        (q.fetchone(
            "SELECT COUNT(DISTINCT device_id) AS n FROM otto_telemetry_events "
            "WHERE day >= %s AND device_id IS NOT NULL",
            (start,),
        ) or {}).get("n") or 0
    )
    total_errors = sum(by_kind.values())
    return {
        "total_errors": total_errors,
        "errors_by_kind": [{"kind": k, "n": v} for k, v in sorted(by_kind.items(), key=lambda kv: kv[1], reverse=True)],
        "errors_per_active_device": round(total_errors / active_devices, 3) if active_devices else None,
    }


# ── Setup & Onboarding ──────────────────────────────────────────────────────


def setup(window: str) -> dict[str, Any]:
    start = q.day_bounds(window)[0]

    # Permission grant rate per gate — the silent top-of-funnel killer.
    perm_rows = q.fetchall(
        "SELECT props FROM otto_telemetry_events WHERE event='permission' AND day >= %s LIMIT 20000",
        (start,),
    )
    perms: dict[str, dict[str, int]] = {}
    for r in perm_rows:
        try:
            p = json.loads(r.get("props") or "{}") or {}
        except (TypeError, ValueError):
            continue
        kind = str(p.get("kind") or "unknown")
        slot = perms.setdefault(kind, {"granted": 0, "total": 0})
        slot["total"] += 1
        if p.get("granted"):
            slot["granted"] += 1
    permission_grant = [
        {"kind": k, "granted": v["granted"], "total": v["total"], "grant_rate": q.rate(v["granted"], v["total"], 2)}
        for k, v in sorted(perms.items())
    ]

    # Onboarding funnel: reached vs completed vs abandoned, per step.
    onb_rows = q.fetchall(
        "SELECT props FROM otto_telemetry_events WHERE event='onboarding' AND day >= %s LIMIT 20000",
        (start,),
    )
    steps: dict[str, dict[str, int]] = {}
    for r in onb_rows:
        try:
            p = json.loads(r.get("props") or "{}") or {}
        except (TypeError, ValueError):
            continue
        step = str(p.get("step") or "unknown")
        status = str(p.get("status") or "reached")
        slot = steps.setdefault(step, {"reached": 0, "completed": 0, "abandoned": 0})
        if status in slot:
            slot[status] += 1
    onboarding = [{"step": k, **v} for k, v in steps.items()]

    accounts = q.fetchall(
        "SELECT props FROM otto_telemetry_events WHERE event='account' AND day >= %s LIMIT 20000",
        (start,),
    )
    providers: dict[str, int] = {}
    for r in accounts:
        try:
            p = json.loads(r.get("props") or "{}") or {}
        except (TypeError, ValueError):
            continue
        if p.get("action") == "connected":
            prov = str(p.get("provider") or "unknown")
            providers[prov] = providers.get(prov, 0) + 1

    return {
        "permission_grant": permission_grant,
        "onboarding": onboarding,
        "accounts_connected": [{"provider": k, "n": v} for k, v in sorted(providers.items(), key=lambda kv: kv[1], reverse=True)],
    }


# ── Learning (does Otto get smarter on repeats) ─────────────────────────────


def learning(window: str) -> dict[str, Any]:
    start = q.day_bounds(window)[0]
    rows = q.fetchall(
        "SELECT from_recipe, COUNT(*) AS n, "
        "SUM(COALESCE(total_ms,0)) AS ms, SUM(COALESCE(cost_usd,0)) AS cost, "
        "SUM(CASE WHEN total_ms IS NOT NULL THEN 1 ELSE 0 END) AS ms_n "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s GROUP BY from_recipe",
        (start,),
    )
    repeat = {"n": 0, "ms": 0.0, "cost": 0.0, "ms_n": 0}
    first = {"n": 0, "ms": 0.0, "cost": 0.0, "ms_n": 0}
    for r in rows:
        bucket = repeat if r.get("from_recipe") in (1, True) else first
        bucket["n"] += int(r["n"] or 0)
        bucket["ms"] += float(r["ms"] or 0)
        bucket["cost"] += float(r["cost"] or 0)
        bucket["ms_n"] += int(r["ms_n"] or 0)
    total = repeat["n"] + first["n"]

    def _avg(b: dict[str, float], key: str, denom_key: str) -> float | None:
        denom = b[denom_key]
        return round(b[key] / denom, 4) if denom else None

    return {
        "repeat_share": q.rate(repeat["n"], total, 3),
        "repeat": {
            "tasks": repeat["n"],
            "avg_total_ms": _avg(repeat, "ms", "ms_n"),
            "avg_cost_usd": _avg(repeat, "cost", "n"),
        },
        "first_time": {
            "tasks": first["n"],
            "avg_total_ms": _avg(first, "ms", "ms_n"),
            "avg_cost_usd": _avg(first, "cost", "n"),
        },
    }
