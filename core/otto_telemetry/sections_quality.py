"""
sections_quality.py — Quality, Latency, and the intent×app Matrix.

Reliable quality and latency are the two determining factors for a computer-use
agent, so these are the dashboard's core. Quality slices success and failure
reasons by where they happen; Latency breaks the wall-clock a user feels into
its stages and separates the fast structured path from the slow vision path.
"""

from __future__ import annotations

import json
from typing import Any

from core.otto_telemetry import queries as q

# Latency components, in the order the user experiences them within a task.
_LATENCY_COLUMNS = ("ttfa_ms", "total_ms", "perceive_ms", "model_ms", "act_ms", "verify_ms")


# ── Quality ─────────────────────────────────────────────────────────────────


def quality(window: str) -> dict[str, Any]:
    start = q.day_bounds(window)[0]

    totals = q.fetchone(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS ok, "
        "SUM(CASE WHEN outcome='partial' THEN 1 ELSE 0 END) AS partial, "
        "SUM(CASE WHEN outcome='failed' THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN outcome='stopped' THEN 1 ELSE 0 END) AS stopped "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s",
        (start,),
    ) or {}
    n = int(totals.get("n") or 0)

    by_intent = q.fetchall(
        "SELECT COALESCE(intent_category,'other') AS intent, COUNT(*) AS n, "
        "SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS ok "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s "
        "GROUP BY intent_category ORDER BY n DESC LIMIT 15",
        (start,),
    )
    for r in by_intent:
        r["success_rate"] = q.rate(int(r["ok"]), int(r["n"]))

    failure_reasons = q.fetchall(
        "SELECT failure_reason AS reason, COUNT(*) AS n FROM otto_telemetry_events "
        "WHERE event='task' AND day >= %s AND outcome != 'success' "
        "AND failure_reason IS NOT NULL AND failure_reason != 'none' "
        "GROUP BY failure_reason ORDER BY n DESC",
        (start,),
    )

    # accepted / intervened pulled from props (not hot columns) — read the small
    # set of failed/finished rows and tally booleans app-side to stay portable.
    behaviour = q.fetchone(
        "SELECT COUNT(*) AS n, "
        "SUM(from_recipe) AS from_recipe "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s",
        (start,),
    ) or {}

    return {
        "totals": {
            "tasks": n,
            "success": int(totals.get("ok") or 0),
            "partial": int(totals.get("partial") or 0),
            "failed": int(totals.get("failed") or 0),
            "stopped": int(totals.get("stopped") or 0),
            "success_rate": q.rate(int(totals.get("ok") or 0), n),
        },
        "success_by_intent": by_intent,
        "failure_reasons": failure_reasons,
        "from_recipe_share": q.rate(int(behaviour.get("from_recipe") or 0), n, 3),
    }


# ── Latency ─────────────────────────────────────────────────────────────────


def _component_breakdown(start: str) -> dict[str, Any]:
    """Median ms in each stage — where the time goes inside a task."""
    out: dict[str, Any] = {}
    for col in ("perceive_ms", "model_ms", "act_ms", "verify_ms"):
        vals = q.task_latency_values(start, col)
        out[col] = {"p50": q.percentile(vals, 50), "p95": q.percentile(vals, 95)}
    return out


def _path_share(start: str) -> dict[str, Any]:
    """Fast structured path (AX/DOM) vs the slow vision path, by step counts."""
    row = q.fetchone(
        "SELECT SUM(COALESCE(vision_steps,0)) AS vision, SUM(COALESCE(step_count,0)) AS total "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s",
        (start,),
    ) or {}
    vision = int(row.get("vision") or 0)
    total = int(row.get("total") or 0)
    return {
        "vision_steps": vision,
        "total_steps": total,
        "vision_share": q.rate(vision, total, 3),
        "fast_share": q.rate(total - vision, total, 3) if total else None,
    }


def latency(window: str) -> dict[str, Any]:
    start = q.day_bounds(window)[0]

    headline = {}
    for col in ("total_ms", "ttfa_ms"):
        vals = q.task_latency_values(start, col)
        headline[col] = {
            "p50": q.percentile(vals, 50),
            "p95": q.percentile(vals, 95),
            "p99": q.percentile(vals, 99),
            "samples": len(vals),
        }

    # P95 total per day — is it getting slower?
    timeseries = []
    for day in q.day_series(window):
        vals = [
            float(r["v"])
            for r in q.fetchall(
                "SELECT total_ms AS v FROM otto_telemetry_events "
                "WHERE event='task' AND day = %s AND total_ms IS NOT NULL",
                (day,),
            )
        ]
        timeseries.append({"day": day, "p50": q.percentile(vals, 50), "p95": q.percentile(vals, 95)})

    return {
        "headline": headline,
        "components": _component_breakdown(start),
        "path": _path_share(start),
        "p95_timeseries": timeseries,
        "by_model": _model_latency(start),
    }


def _model_latency(start: str) -> list[dict[str, Any]]:
    """Per-model average latency, aggregated from each task's props.models list
    (which model ran, how long, how many calls). Read app-side because the list
    is nested JSON, not a hot column. Bounded to recent task rows for cost."""
    rows = q.fetchall(
        "SELECT props FROM otto_telemetry_events WHERE event='task' AND day >= %s LIMIT 5000",
        (start,),
    )
    agg: dict[str, dict[str, float]] = {}
    for r in rows:
        try:
            models = (json.loads(r.get("props") or "{}") or {}).get("models") or []
        except (TypeError, ValueError):
            continue
        for m in models:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "unknown")[:60]
            slot = agg.setdefault(name, {"ms": 0.0, "calls": 0.0})
            slot["ms"] += float(m.get("ms") or 0)
            slot["calls"] += float(m.get("calls") or 0)
    out = [
        {
            "model": name,
            "calls": int(v["calls"]),
            "avg_ms": round(v["ms"] / v["calls"], 1) if v["calls"] else None,
        }
        for name, v in agg.items()
    ]
    out.sort(key=lambda x: x["calls"], reverse=True)
    return out


# ── Matrix (intent × app — the one table that ranks what to fix) ─────────────


def matrix(window: str) -> dict[str, Any]:
    """One row per (intent, app): volume, success, P95 time, vision share, cost.
    Sorted by volume so the worst high-frequency cell is the top thing to fix."""
    start = q.day_bounds(window)[0]
    rows = q.fetchall(
        "SELECT COALESCE(intent_category,'other') AS intent, COALESCE(app,'(unknown)') AS app, "
        "COUNT(*) AS n, "
        "SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS ok, "
        "SUM(COALESCE(vision_steps,0)) AS vision, SUM(COALESCE(step_count,0)) AS steps, "
        "SUM(COALESCE(cost_usd,0)) AS cost "
        "FROM otto_telemetry_events WHERE event='task' AND day >= %s "
        "GROUP BY intent_category, app HAVING COUNT(*) >= 1 ORDER BY n DESC LIMIT 60",
        (start,),
    )
    out = []
    for r in rows:
        n = int(r["n"])
        vals = q.task_latency_values(start, "total_ms", app=r["app"], intent=r["intent"])
        out.append(
            {
                "intent": r["intent"],
                "app": r["app"],
                "tasks": n,
                "success_rate": q.rate(int(r["ok"]), n),
                "p95_total_ms": q.percentile(vals, 95),
                "vision_share": q.rate(int(r["vision"] or 0), int(r["steps"] or 0), 3),
                "cost_per_task_usd": round(float(r["cost"] or 0) / n, 4) if n else None,
            }
        )
    return {"rows": out}
