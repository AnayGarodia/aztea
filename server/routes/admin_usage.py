"""
admin_usage.py — scannable observability surface.

OWNS: three admin-only HTTP endpoints that turn the existing tables (jobs,
      transactions, mcp_invocation_log, tool_invocation_metrics, users,
      api_keys, auto_hire_decisions, agents) into a digest, an entity
      inspector, and a small set of pre-canned views.

      Exposed routes (all admin-scope + IP-allowlist gated):

        GET  /admin/usage/digest?window=24h|7d|30d
             High-level rollup of calls, spend, top/failing agents, user
             churn, and auto-hire stats — with trend deltas vs the prior
             window.

        GET  /admin/usage/inspect?entity=<entity>&id=<id>
             Per-entity drill-down. Entities:
               agent     — calls / success rate / latency p50,p95 / revenue
               user      — installs, first/last call, lifetime spend,
                           top agents used
               job       — single job row decorated with rating/dispute/
                           message counts
               decision  — single auto_hire_decisions row by decision_id

        GET  /admin/usage/query?view=<view>&window=&filter=&limit=&sort=
             Pre-canned views. View enum:
               no_match          — top intent clusters with no agent match
               failures          — recent failed MCP calls with error codes
               agent_health      — every agent's success rate + call count
               user_activity     — last call / spend per user
               top_agents        — agents sorted by call count
               dormant_users     — users with no recent calls
               spend_by_user     — total spend per user, sorted desc
               spend_by_agent    — total revenue per agent, sorted desc
               latency_outliers  — slowest p95 agents
               recent_decisions  — most recent auto-hire decisions

NOT OWNS: the digest's rendering / textual presentation (clients format).
INVARIANTS:
  - Unknown ``window``, ``entity``, or ``view`` values raise 400. Silent
    fallback to empty would mask typos.
  - Every query is read-only. No INSERT / UPDATE / DELETE.
  - All money is integer cents on the wire.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core import db as _db

logger = logging.getLogger(__name__)


# ── Window parsing ─────────────────────────────────────────────────────────

# Why named: SQL window comparisons need an absolute ISO timestamp string,
# the trend delta needs an "older than this" cutoff, and we want one place
# that maps the small set of allowed shorthand values onto seconds.
_ALLOWED_WINDOWS: dict[str, int] = {
    "24h": 24 * 60 * 60,
    "7d":  7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_bounds(window: str) -> tuple[str, str, str]:
    """Return (window_start_iso, prior_start_iso, now_iso) for trend math.

    Why: every digest metric is reported as ``{value, delta_pct}``; the prior
    bucket is the same-sized window immediately before the current one.
    """
    if window not in _ALLOWED_WINDOWS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown window {window!r}. Allowed: "
                f"{sorted(_ALLOWED_WINDOWS.keys())}"
            ),
        )
    span = _ALLOWED_WINDOWS[window]
    now = _now()
    window_start = now - timedelta(seconds=span)
    prior_start = window_start - timedelta(seconds=span)
    return _iso(window_start), _iso(prior_start), _iso(now)


def _pct_delta(current: float, prior: float) -> float | None:
    """Pure: percentage change from prior to current. None when prior is zero or both are zero."""
    if prior == 0:
        return None if current == 0 else float("inf")
    return round((current - prior) / prior * 100.0, 1)


def _trend(value: float, prior: float) -> dict[str, Any]:
    delta = _pct_delta(value, prior)
    return {
        "value": value,
        "prior": prior,
        "delta_pct": delta if delta != float("inf") else None,
    }


# ── DB helpers ─────────────────────────────────────────────────────────────


def _fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn: _db.DbConnection = _db.get_raw_connection(_db.DB_PATH)
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    return [dict(r) for r in rows]


def _fetchone(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = _fetchall(sql, params)
    return rows[0] if rows else None


# ── Percentiles (app-side; SQLite has no percentile_cont) ──────────────────


def _percentile(values: list[float], pct: float) -> float | None:
    """Pure: nearest-rank percentile. ``pct`` is 0-100. Returns None on empty input.

    Why: SQLite ships without percentile_cont and we don't want to depend on
    numpy for one number. Nearest-rank is fine for an observability digest.
    """
    if not values:
        return None
    if pct <= 0:
        return float(min(values))
    if pct >= 100:
        return float(max(values))
    sorted_vals = sorted(float(v) for v in values)
    index = int(round(pct / 100.0 * len(sorted_vals) + 0.5)) - 1
    index = max(0, min(index, len(sorted_vals) - 1))
    return float(sorted_vals[index])


# ── Digest sections ────────────────────────────────────────────────────────


def _digest_calls(window_start: str, prior_start: str) -> dict[str, Any]:
    """Counts for the current window and a same-sized prior window."""
    cur = _fetchone(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS success,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled
        FROM jobs
        WHERE created_at >= %s
        """,
        (window_start,),
    ) or {}
    prior = _fetchone(
        """
        SELECT COUNT(*) AS total
        FROM jobs
        WHERE created_at >= %s AND created_at < %s
        """,
        (prior_start, window_start),
    ) or {}
    total = int(cur.get("total") or 0)
    success = int(cur.get("success") or 0)
    failed = int(cur.get("failed") or 0)
    return {
        "total":     _trend(total, int(prior.get("total") or 0)),
        "success":   success,
        "failed":    failed,
        "cancelled": int(cur.get("cancelled") or 0),
        "success_rate": round(success / total, 3) if total else None,
    }


def _digest_spend(window_start: str, prior_start: str) -> dict[str, Any]:
    cur = _fetchone(
        """
        SELECT
            COALESCE(SUM(amount_cents), 0) AS total_cents,
            COUNT(DISTINCT wallet_id)      AS unique_wallets
        FROM transactions
        WHERE type = 'charge' AND created_at >= %s
        """,
        (window_start,),
    ) or {}
    prior = _fetchone(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS total_cents
        FROM transactions
        WHERE type = 'charge'
          AND created_at >= %s AND created_at < %s
        """,
        (prior_start, window_start),
    ) or {}
    return {
        "total_cents":    _trend(
            int(cur.get("total_cents") or 0),
            int(prior.get("total_cents") or 0),
        ),
        "unique_wallets": int(cur.get("unique_wallets") or 0),
    }


def _digest_top_agents(window_start: str, limit: int = 5) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT j.agent_id,
               COALESCE(a.name, j.agent_id) AS name,
               COUNT(*) AS calls,
               SUM(CASE WHEN j.status='complete' THEN 1 ELSE 0 END) AS success
        FROM jobs j
        LEFT JOIN agents a ON a.agent_id = j.agent_id
        WHERE j.created_at >= %s
        GROUP BY j.agent_id, a.name
        ORDER BY calls DESC
        LIMIT %s
        """,
        (window_start, limit),
    )


def _digest_failing_agents(window_start: str, min_calls: int = 3, limit: int = 5) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT j.agent_id,
               COALESCE(a.name, j.agent_id) AS name,
               COUNT(*) AS calls,
               SUM(CASE WHEN j.status='complete' THEN 1 ELSE 0 END) AS success,
               ROUND(1.0 * SUM(CASE WHEN j.status='complete' THEN 1 ELSE 0 END) / COUNT(*), 3) AS success_rate
        FROM jobs j
        LEFT JOIN agents a ON a.agent_id = j.agent_id
        WHERE j.created_at >= %s
        GROUP BY j.agent_id, a.name
        HAVING COUNT(*) >= %s
        ORDER BY success_rate ASC, calls DESC
        LIMIT %s
        """,
        (window_start, min_calls, limit),
    )


def _digest_users(window_start: str, dormant_cutoff: str) -> dict[str, Any]:
    new_installed = _fetchone(
        "SELECT COUNT(*) AS n FROM users WHERE created_at >= %s",
        (window_start,),
    ) or {}
    first_call = _fetchone(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT caller_owner_id, MIN(created_at) AS first_at
            FROM jobs
            GROUP BY caller_owner_id
        ) WHERE first_at >= %s
        """,
        (window_start,),
    ) or {}
    dormant = _fetchone(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT u.user_id, MAX(j.created_at) AS last_at
            FROM users u
            LEFT JOIN jobs j ON j.caller_owner_id = 'user:' || u.user_id
            GROUP BY u.user_id
            HAVING last_at IS NOT NULL AND last_at < %s
        )
        """,
        (dormant_cutoff,),
    ) or {}
    return {
        "new_installed":  int(new_installed.get("n") or 0),
        "first_call":     int(first_call.get("n") or 0),
        "dormant_14d":    int(dormant.get("n") or 0),
    }


def _digest_auto_hire(window_start: str) -> dict[str, Any]:
    summary = _fetchone(
        """
        SELECT
            COUNT(*) AS invocations,
            SUM(auto_invoked) AS auto_invoked,
            SUM(CASE WHEN reason='no_match' THEN 1 ELSE 0 END) AS no_match,
            SUM(dry_run) AS dry_run_count
        FROM auto_hire_decisions
        WHERE created_at >= %s
        """,
        (window_start,),
    ) or {}
    top_no_match = _fetchall(
        """
        SELECT intent_hash, COUNT(*) AS hits, MAX(intent_text) AS example_intent
        FROM auto_hire_decisions
        WHERE reason='no_match' AND created_at >= %s
        GROUP BY intent_hash
        ORDER BY hits DESC
        LIMIT 5
        """,
        (window_start,),
    )
    return {
        "invocations":        int(summary.get("invocations") or 0),
        "auto_invoked":       int(summary.get("auto_invoked") or 0),
        "no_match":           int(summary.get("no_match") or 0),
        "dry_run_count":      int(summary.get("dry_run_count") or 0),
        "top_no_match_intents": top_no_match,
    }


# ── Inspect entities ───────────────────────────────────────────────────────


def _inspect_agent(agent_id: str) -> dict[str, Any]:
    agent = _fetchone(
        """
        SELECT agent_id, name, price_per_call_usd, total_calls, successful_calls,
               avg_latency_ms, status, review_status
        FROM agents WHERE agent_id = %s
        """,
        (agent_id,),
    )
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found.")
    counts = _fetchone(
        """
        SELECT COUNT(*) AS calls,
               SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS success,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
        FROM jobs WHERE agent_id = %s
        """,
        (agent_id,),
    ) or {}
    latencies = [
        float(r["latency_ms"])
        for r in _fetchall(
            "SELECT latency_ms FROM tool_invocation_metrics WHERE agent_id = %s",
            (agent_id,),
        )
        if r.get("latency_ms") is not None
    ]
    revenue = _fetchone(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS revenue_cents, COUNT(*) AS payouts
        FROM transactions
        WHERE type = 'payout' AND agent_id = %s
        """,
        (agent_id,),
    ) or {}
    calls = int(counts.get("calls") or 0)
    return {
        "agent_id":       agent.get("agent_id"),
        "name":           agent.get("name"),
        "status":         agent.get("status"),
        "review_status":  agent.get("review_status"),
        "price_per_call_usd": agent.get("price_per_call_usd"),
        "calls":          calls,
        "success":        int(counts.get("success") or 0),
        "failed":         int(counts.get("failed") or 0),
        "success_rate":   round(int(counts.get("success") or 0) / calls, 3) if calls else None,
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "revenue_cents":  int(revenue.get("revenue_cents") or 0),
        "payout_count":   int(revenue.get("payouts") or 0),
    }


def _inspect_user(user_id: str) -> dict[str, Any]:
    user = _fetchone(
        "SELECT user_id, username, email, created_at, status FROM users WHERE user_id = %s",
        (user_id,),
    )
    if user is None:
        raise HTTPException(status_code=404, detail=f"User {user_id!r} not found.")
    owner = f"user:{user_id}"
    jobs_summary = _fetchone(
        """
        SELECT COUNT(*) AS calls,
               MIN(created_at) AS first_call,
               MAX(created_at) AS last_call
        FROM jobs WHERE caller_owner_id = %s
        """,
        (owner,),
    ) or {}
    spend = _fetchone(
        """
        SELECT COALESCE(SUM(t.amount_cents), 0) AS spend_cents
        FROM transactions t
        JOIN wallets w ON w.wallet_id = t.wallet_id
        WHERE t.type = 'charge' AND w.owner_id = %s
        """,
        (owner,),
    ) or {}
    top_agents = _fetchall(
        """
        SELECT j.agent_id, COALESCE(a.name, j.agent_id) AS name, COUNT(*) AS calls
        FROM jobs j
        LEFT JOIN agents a ON a.agent_id = j.agent_id
        WHERE j.caller_owner_id = %s
        GROUP BY j.agent_id, a.name
        ORDER BY calls DESC
        LIMIT 5
        """,
        (owner,),
    )
    return {
        "user_id":      user.get("user_id"),
        "username":     user.get("username"),
        "email":        user.get("email"),
        "status":       user.get("status"),
        "installed_at": user.get("created_at"),
        "first_call":   jobs_summary.get("first_call"),
        "last_call":    jobs_summary.get("last_call"),
        "total_calls":  int(jobs_summary.get("calls") or 0),
        "spend_cents":  int(spend.get("spend_cents") or 0),
        "top_agents":   top_agents,
    }


def _inspect_job(job_id: str) -> dict[str, Any]:
    job = _fetchone(
        """
        SELECT job_id, agent_id, caller_owner_id, agent_owner_id, status,
               price_cents, caller_charge_cents, created_at, completed_at,
               settled_at, origin, quality_score, dispute_outcome, retry_count,
               timeout_count, error_message
        FROM jobs WHERE job_id = %s
        """,
        (job_id,),
    )
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found.")
    rating = _fetchone(
        "SELECT rating, created_at FROM job_quality_ratings WHERE job_id = %s ORDER BY created_at DESC LIMIT 1",
        (job_id,),
    )
    dispute = _fetchone(
        "SELECT dispute_id, status, outcome, filed_at, resolved_at FROM disputes WHERE job_id = %s LIMIT 1",
        (job_id,),
    )
    message_count = _fetchone(
        "SELECT COUNT(*) AS n FROM job_messages WHERE job_id = %s",
        (job_id,),
    ) or {}
    return {
        "job":           job,
        "rating":        rating,
        "dispute":       dispute,
        "message_count": int(message_count.get("n") or 0),
    }


def _inspect_decision(decision_id: str) -> dict[str, Any]:
    row = _fetchone(
        """
        SELECT decision_id, caller_owner_id, caller_key_id, intent_text,
               intent_hash, auto_invoked, dry_run, reason, chosen_agent_id,
               confidence, candidates_json, resulting_job_id, created_at
        FROM auto_hire_decisions WHERE decision_id = %s
        """,
        (decision_id,),
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Decision {decision_id!r} not found."
        )
    candidates_raw = row.get("candidates_json")
    try:
        row["candidates"] = json.loads(candidates_raw) if candidates_raw else []
    except (TypeError, ValueError):
        row["candidates"] = []
    row.pop("candidates_json", None)
    return row


_INSPECT_DISPATCH: dict[str, Callable[[str], dict[str, Any]]] = {
    "agent":    _inspect_agent,
    "user":     _inspect_user,
    "job":      _inspect_job,
    "decision": _inspect_decision,
}


# ── Views ──────────────────────────────────────────────────────────────────


def _view_no_match(window_start: str, limit: int) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT intent_hash, COUNT(*) AS hits, MAX(intent_text) AS example_intent,
               MIN(created_at) AS first_seen, MAX(created_at) AS last_seen
        FROM auto_hire_decisions
        WHERE reason = 'no_match' AND created_at >= %s
        GROUP BY intent_hash
        ORDER BY hits DESC
        LIMIT %s
        """,
        (window_start, limit),
    )


def _view_failures(window_start: str, limit: int) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT id, agent_id, tool_name, error_code, duration_ms, invoked_at, caller_key_id
        FROM mcp_invocation_log
        WHERE success = 0 AND invoked_at >= %s
        ORDER BY invoked_at DESC
        LIMIT %s
        """,
        (window_start, limit),
    )


def _view_agent_health(window_start: str, limit: int) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT j.agent_id, COALESCE(a.name, j.agent_id) AS name,
               COUNT(*) AS calls,
               SUM(CASE WHEN j.status='complete' THEN 1 ELSE 0 END) AS success,
               ROUND(1.0 * SUM(CASE WHEN j.status='complete' THEN 1 ELSE 0 END) / COUNT(*), 3) AS success_rate
        FROM jobs j
        LEFT JOIN agents a ON a.agent_id = j.agent_id
        WHERE j.created_at >= %s
        GROUP BY j.agent_id, a.name
        ORDER BY success_rate ASC, calls DESC
        LIMIT %s
        """,
        (window_start, limit),
    )


def _view_user_activity(window_start: str, limit: int) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT u.user_id, u.email, u.created_at AS installed_at,
               MAX(j.created_at) AS last_call,
               COUNT(j.job_id) AS calls,
               COALESCE(SUM(j.caller_charge_cents), 0) AS spend_cents
        FROM users u
        LEFT JOIN jobs j ON j.caller_owner_id = 'user:' || u.user_id
                        AND j.created_at >= %s
        GROUP BY u.user_id, u.email, u.created_at
        ORDER BY last_call DESC NULLS LAST, calls DESC
        LIMIT %s
        """,
        (window_start, limit),
    )


def _view_top_agents(window_start: str, limit: int) -> list[dict[str, Any]]:
    return _digest_top_agents(window_start, limit)


def _view_dormant_users(cutoff: str, limit: int) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT u.user_id, u.email, u.created_at AS installed_at,
               MAX(j.created_at) AS last_call,
               COUNT(j.job_id) AS lifetime_calls
        FROM users u
        LEFT JOIN jobs j ON j.caller_owner_id = 'user:' || u.user_id
        GROUP BY u.user_id, u.email, u.created_at
        HAVING last_call IS NOT NULL AND last_call < %s
        ORDER BY last_call ASC
        LIMIT %s
        """,
        (cutoff, limit),
    )


def _view_spend_by_user(window_start: str, limit: int) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT w.owner_id,
               COALESCE(SUM(t.amount_cents), 0) AS spend_cents,
               COUNT(*) AS charges
        FROM transactions t
        JOIN wallets w ON w.wallet_id = t.wallet_id
        WHERE t.type = 'charge' AND t.created_at >= %s
        GROUP BY w.owner_id
        ORDER BY spend_cents DESC
        LIMIT %s
        """,
        (window_start, limit),
    )


def _view_spend_by_agent(window_start: str, limit: int) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT t.agent_id,
               COALESCE(a.name, t.agent_id) AS name,
               COALESCE(SUM(t.amount_cents), 0) AS revenue_cents,
               COUNT(*) AS payouts
        FROM transactions t
        LEFT JOIN agents a ON a.agent_id = t.agent_id
        WHERE t.type = 'payout' AND t.agent_id IS NOT NULL AND t.created_at >= %s
        GROUP BY t.agent_id, a.name
        ORDER BY revenue_cents DESC
        LIMIT %s
        """,
        (window_start, limit),
    )


def _view_latency_outliers(window_start: str, limit: int) -> list[dict[str, Any]]:
    rows = _fetchall(
        """
        SELECT agent_id, latency_ms
        FROM tool_invocation_metrics
        WHERE created_at >= %s
        """,
        (window_start,),
    )
    buckets: dict[str, list[float]] = {}
    for r in rows:
        agent = str(r.get("agent_id") or "")
        latency = r.get("latency_ms")
        if agent and latency is not None:
            buckets.setdefault(agent, []).append(float(latency))
    out = []
    for agent, vals in buckets.items():
        p95 = _percentile(vals, 95)
        if p95 is None:
            continue
        out.append({
            "agent_id":       agent,
            "samples":        len(vals),
            "latency_p50_ms": _percentile(vals, 50),
            "latency_p95_ms": p95,
        })
    out.sort(key=lambda r: r["latency_p95_ms"], reverse=True)
    return out[:limit]


def _view_recent_decisions(window_start: str, limit: int) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT decision_id, intent_text, auto_invoked, dry_run, reason,
               chosen_agent_id, confidence, resulting_job_id, created_at
        FROM auto_hire_decisions
        WHERE created_at >= %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (window_start, limit),
    )


_VIEW_DISPATCH: dict[str, Callable[[str, int], list[dict[str, Any]]]] = {
    "no_match":         _view_no_match,
    "failures":         _view_failures,
    "agent_health":     _view_agent_health,
    "user_activity":    _view_user_activity,
    "top_agents":       _view_top_agents,
    "dormant_users":    _view_dormant_users,
    "spend_by_user":    _view_spend_by_user,
    "spend_by_agent":   _view_spend_by_agent,
    "latency_outliers": _view_latency_outliers,
    "recent_decisions": _view_recent_decisions,
}


# ── Router factory ─────────────────────────────────────────────────────────


def _default_dormant_cutoff() -> str:
    return _iso(_now() - timedelta(days=14))


def create_router(
    *,
    require_api_key: Callable[..., Any],
    require_scope: Callable[..., None],
    require_admin_ip_allowlist: Callable[[Request], None],
) -> APIRouter:
    """Build the admin-usage router with caller-supplied auth helpers.

    Why factory: the helpers live in ``server.application`` (the sharded
    namespace). Importing them at module-load time would create a cycle.
    """
    router = APIRouter()

    def _gate(caller: Any, request: Request) -> None:
        require_scope(caller, "admin", detail="This endpoint requires admin scope.")
        require_admin_ip_allowlist(request)

    @router.get("/admin/usage/digest")
    def usage_digest(
        request: Request,
        window: str = Query("24h"),
        caller: Any = Depends(require_api_key),
    ) -> JSONResponse:
        _gate(caller, request)
        window_start, prior_start, as_of = _window_bounds(window)
        dormant_cutoff = _default_dormant_cutoff()
        return JSONResponse(content={
            "window":         window,
            "as_of":          as_of,
            "window_start":   window_start,
            "calls":          _digest_calls(window_start, prior_start),
            "spend":          _digest_spend(window_start, prior_start),
            "top_agents":     _digest_top_agents(window_start),
            "failing_agents": _digest_failing_agents(window_start),
            "users":          _digest_users(window_start, dormant_cutoff),
            "auto_hire":      _digest_auto_hire(window_start),
        })

    @router.get("/admin/usage/inspect")
    def usage_inspect(
        request: Request,
        entity: str = Query(...),
        id: str = Query(..., min_length=1),
        caller: Any = Depends(require_api_key),
    ) -> JSONResponse:
        _gate(caller, request)
        handler = _INSPECT_DISPATCH.get(entity)
        if handler is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown entity {entity!r}. Allowed: "
                    f"{sorted(_INSPECT_DISPATCH.keys())}"
                ),
            )
        return JSONResponse(content={"entity": entity, "id": id, "data": handler(id)})

    @router.get("/admin/usage/query")
    def usage_query(
        request: Request,
        view: str = Query(...),
        window: str = Query("7d"),
        limit: int = Query(50, ge=1, le=500),
        caller: Any = Depends(require_api_key),
    ) -> JSONResponse:
        _gate(caller, request)
        handler = _VIEW_DISPATCH.get(view)
        if handler is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown view {view!r}. Allowed: "
                    f"{sorted(_VIEW_DISPATCH.keys())}"
                ),
            )
        # Dormant uses an absolute 14-day cutoff regardless of window.
        if view == "dormant_users":
            rows = handler(_default_dormant_cutoff(), limit)
        else:
            window_start, _prior, _now_iso = _window_bounds(window)
            rows = handler(window_start, limit)
        return JSONResponse(content={
            "view":   view,
            "window": window if view != "dormant_users" else "14d",
            "limit":  limit,
            "rows":   rows,
        })

    return router
