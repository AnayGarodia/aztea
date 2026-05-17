"""
Lightweight observability helpers for Aztea.

Provides:
  - timed()      context manager that records wall-clock milliseconds
  - record_call  persists a row to tool_invocation_metrics (best-effort)

Designed to add zero overhead when the metrics table isn't available yet
(i.e., before migration 0028 runs) and to never raise.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator

from core import db as _db

logger = logging.getLogger(__name__)


class CallTimer:
    """Collects timing data accumulated inside a `timed()` block."""

    def __init__(self) -> None:
        self.elapsed_ms: float = 0.0

    def __repr__(self) -> str:  # noqa: D105
        return f"<CallTimer {self.elapsed_ms:.1f} ms>"


@contextmanager
def timed() -> Generator[CallTimer, None, None]:
    """Context manager that measures wall-clock time in milliseconds.

    Usage::

        with timed() as t:
            result = do_work()
        print(t.elapsed_ms)
    """
    timer = CallTimer()
    start = time.perf_counter()
    try:
        yield timer
    finally:
        timer.elapsed_ms = (time.perf_counter() - start) * 1000.0


def record_call(
    *,
    agent_id: str,
    caller_id: str | None,
    latency_ms: float,
    bytes_in: int = 0,
    bytes_out: int = 0,
    cached: bool = False,
) -> None:
    """Persist a call metric row.  Never raises — failures are logged only.

    Silently skips if the metrics table doesn't exist yet (pre-migration).
    """
    try:
        conn: _db.DbConnection = _db.get_raw_connection(_db.DB_PATH)
        conn.execute(
            """
            INSERT INTO tool_invocation_metrics
                (agent_id, caller_id, latency_ms, bytes_in, bytes_out, cached, created_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s)
            """,
            (agent_id, caller_id, latency_ms, bytes_in, bytes_out, int(cached),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except _db.OperationalError as exc:
        # Table missing pre-migration — ignore.
        if "no such table" not in str(exc):
            logger.debug("observability: metric write failed: %s", exc)
    except Exception as exc:  # pragma: no cover
        logger.debug("observability: metric write failed: %s", exc)


# ---------------------------------------------------------------------------
# Payment event counters — incremented in core/payments/base.py
# ---------------------------------------------------------------------------
# Using a lazy-import pattern (same as part_001.py) so this module doesn't
# hard-require prometheus_client in environments that don't install it.

try:
    from prometheus_client import Counter as _PCounter
    from prometheus_client import Histogram as _PHistogram

    payment_charges_total = _PCounter(
        "aztea_payment_charges_total",
        "Wallet pre-call charge outcomes",
        ["outcome"],  # success | insufficient_balance | wallet_not_found | spend_limit_exceeded
    )
    payment_payouts_total = _PCounter(
        "aztea_payment_payouts_total",
        "Post-call payout outcomes",
        ["outcome"],  # success | skipped_refund_exists
    )
    payment_refunds_total = _PCounter(
        "aztea_payment_refunds_total",
        "Post-call refund outcomes",
        ["outcome"],  # success | skipped_payout_exists
    )
    # Surfaces a real money-loss event: the agent wallet couldn't absorb a
    # rating-driven clawback (already withdrawn / missing). Operator must
    # reconcile manually — see core/payout_curve.py.
    payout_curve_clawback_total = _PCounter(
        "aztea_payout_curve_clawback_total",
        "Payout-curve clawback outcomes (rating-driven refunds)",
        ["outcome"],  # applied | insufficient_balance | wallet_missing | already_applied
    )
    job_duration_seconds = _PHistogram(
        "job_duration_seconds",
        "End-to-end job latency from creation to terminal state",
        ["agent_id", "status"],  # status: complete | failed
        buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
    )
    builtin_agent_calls_total = _PCounter(
        "builtin_agent_calls_total",
        "Built-in agent dispatch outcomes",
        ["agent_slug", "status"],  # status: success | failure
    )
    wallet_hold_created_total = _PCounter(
        "aztea_wallet_hold_created_total",
        "Reserve-hold rows created on agent payout",
    )
    wallet_hold_released_total = _PCounter(
        "aztea_wallet_hold_released_total",
        "Reserve-hold lifecycle exits",
        ["reason"],  # window_expired | rating_release | rating_clawback | dispute_clawback
    )
    wallet_hold_clawed_total = _PCounter(
        "aztea_wallet_hold_clawed_total",
        "Reserve-hold consumption events (subset of released_total)",
        ["reason"],  # rating_clawback | dispute_clawback
    )
    payout_curve_clawback_skipped_total = _PCounter(
        "aztea_payout_curve_clawback_skipped_total",
        "Payout-curve clawback that fell through to defense-in-depth path. "
        "Should sit at ~0 in steady state — every increment indicates a hold "
        "lifecycle bug or a pre-deploy job rated post-deploy.",
        ["reason"],  # underflow | wallet_missing | no_active_hold
    )
    # Auto-hire routing decisions — labels measure the gap between calls
    # reaching the router and calls actually firing an agent. Outcomes:
    #   auto_invoked    — gate passed; agent ran (or would-run on dry_run path)
    #   gated           — gate failed; refusal returned, no charge
    #   dry_run         — dry_run=true short-circuit; no charge by design
    #   delegation_failed — downstream HTTPException from registry_call
    route_decisions_total = _PCounter(
        "aztea_route_decisions_total",
        "Auto-hire routing decision outcomes (does the model's dry_run reflex "
        "actually fire an agent or short-circuit).",
        ["outcome", "reason"],
    )
    route_latency_seconds = _PHistogram(
        "aztea_route_latency_seconds",
        "Wall-clock time inside the /registry/agents/auto-hire handler. "
        "dry_run latency is the relevant SLI for the reflex-tool framing — "
        "if the p99 climbs above ~0.4s the model stops calling speculatively.",
        ["outcome"],
        buckets=[0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 1.0, 2.5, 5.0],
    )
except ImportError:
    # prometheus_client not installed — use no-op stubs so callers never need IS_PROM guards.
    class _NoopLabels:
        def inc(self, amount: int = 1) -> None:
            pass

        def observe(self, _value: float) -> None:
            pass

    class _NoopMetric:
        def labels(self, **_kwargs) -> "_NoopLabels":
            return _NoopLabels()

        def inc(self, amount: int = 1) -> None:
            pass

        def observe(self, _value: float) -> None:
            pass

    payment_charges_total = _NoopMetric()  # type: ignore[assignment]
    payment_payouts_total = _NoopMetric()  # type: ignore[assignment]
    payment_refunds_total = _NoopMetric()  # type: ignore[assignment]
    payout_curve_clawback_total = _NoopMetric()  # type: ignore[assignment]
    job_duration_seconds = _NoopMetric()  # type: ignore[assignment]
    builtin_agent_calls_total = _NoopMetric()  # type: ignore[assignment]
    wallet_hold_created_total = _NoopMetric()  # type: ignore[assignment]
    wallet_hold_released_total = _NoopMetric()  # type: ignore[assignment]
    wallet_hold_clawed_total = _NoopMetric()  # type: ignore[assignment]
    payout_curve_clawback_skipped_total = _NoopMetric()  # type: ignore[assignment]
    route_decisions_total = _NoopMetric()  # type: ignore[assignment]
    route_latency_seconds = _NoopMetric()  # type: ignore[assignment]


_JOB_TERMINAL_STATUSES = ("complete", "failed")


def record_job_duration(
    agent_id: str | None,
    status: str,
    duration_seconds: float,
) -> None:
    """Observe job latency. Never raises — failures are silent."""
    if status not in _JOB_TERMINAL_STATUSES:
        return
    if duration_seconds < 0:
        return
    try:
        job_duration_seconds.labels(
            agent_id=str(agent_id or "unknown"),
            status=status,
        ).observe(duration_seconds)
    except Exception as exc:  # pragma: no cover
        logger.debug("observability: job duration metric failed: %s", exc)


def record_builtin_agent_call(agent_slug: str | None, status: str) -> None:
    """Increment the per-builtin-agent call counter. Never raises."""
    try:
        builtin_agent_calls_total.labels(
            agent_slug=str(agent_slug or "unknown"),
            status=status,
        ).inc()
    except Exception as exc:  # pragma: no cover
        logger.debug("observability: builtin agent counter failed: %s", exc)


def record_route_decision(
    outcome: str, reason: str, elapsed_seconds: float,
) -> None:
    """Record one auto-hire routing decision + latency. Never raises.

    Called from the /registry/agents/auto-hire handler on every return path
    so we can tell whether the do_specialist_task reflex actually fires an
    agent or short-circuits. Without this counter we ship the reflex-tool
    rewrite blind to whether attach rate moved.
    """
    try:
        route_decisions_total.labels(
            outcome=str(outcome or "unknown"),
            reason=str(reason or "unknown"),
        ).inc()
        if elapsed_seconds >= 0:
            route_latency_seconds.labels(
                outcome=str(outcome or "unknown"),
            ).observe(elapsed_seconds)
    except Exception as exc:  # pragma: no cover
        logger.debug("observability: route decision metric failed: %s", exc)



# ---------------------------------------------------------------------------
# Auto-hire decision retention
# ---------------------------------------------------------------------------
# Why: ``auto_hire_decisions`` grows unbounded under real traffic. The
# retention policy (declared in migration 0050) is: aggregate rows older
# than 90 days into ``auto_hire_decisions_daily``, then DELETE the raw rows.
# The daily rollup is kept indefinitely so trend questions past 90 days
# remain answerable, just at lower granularity.

# Cap on the number of intent_hashes stored per (day, reason, auto_invoked)
# bucket. Picked so a year of rollups stays comfortably under a megabyte
# even for the no_match category.
_ROLLUP_INTENT_HASH_CAP = 50

_DECISION_RETENTION_DAYS = 90


def _decision_rollup_cutoff(now_utc: datetime | None = None) -> str:
    """Pure: ISO timestamp ``_DECISION_RETENTION_DAYS`` ago. Rows at or before this go to the rollup."""
    base = now_utc or datetime.now(timezone.utc)
    cutoff = base - timedelta(days=_DECISION_RETENTION_DAYS)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_rollup_rows(raw_rows: list[dict]) -> list[tuple]:
    """Pure: group raw decision rows by (day, reason, auto_invoked) and return INSERT params.

    Why: SQLite has no per-key array aggregation function, so we read the
    rows back and bucket in Python. Keeps the SQL surface minimal at the
    cost of one extra pass — fine for a once-a-day job.
    """
    import json as _json
    buckets: dict[tuple[str, str | None, int], dict] = {}
    for r in raw_rows:
        day = (r.get("created_at") or "")[:10]
        reason = r.get("reason")
        auto = int(r.get("auto_invoked") or 0)
        key = (day, reason, auto)
        bucket = buckets.setdefault(key, {
            "count": 0, "callers": set(), "hashes": set(),
        })
        bucket["count"] += 1
        caller = r.get("caller_owner_id")
        if caller:
            bucket["callers"].add(caller)
        h = r.get("intent_hash")
        if h and len(bucket["hashes"]) < _ROLLUP_INTENT_HASH_CAP:
            bucket["hashes"].add(h)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        (
            day, reason, auto, bucket["count"], len(bucket["callers"]),
            _json.dumps(sorted(bucket["hashes"])), now_iso,
        )
        for (day, reason, auto), bucket in buckets.items()
    ]


def run_decision_retention() -> dict[str, int]:
    """Side-effect: roll up + delete auto_hire_decisions older than 90 days.

    Returns a small summary dict so callers can log it. Never raises — a
    failure here must not knock the sweeper over. Idempotent: running twice
    in a row is a no-op because the second call finds no eligible rows.
    """
    summary = {"raw_rows_rolled": 0, "rollup_rows_written": 0, "raw_rows_deleted": 0}
    try:
        conn: _db.DbConnection = _db.get_raw_connection(_db.DB_PATH)
        cutoff = _decision_rollup_cutoff()
        rows = conn.execute(
            """
            SELECT created_at, reason, auto_invoked, caller_owner_id, intent_hash
            FROM auto_hire_decisions
            WHERE created_at < %s
            """,
            (cutoff,),
        ).fetchall()
        raw_rows = [dict(r) for r in rows]
        if not raw_rows:
            return summary
        params = _build_rollup_rows(raw_rows)
        for p in params:
            # INSERT OR REPLACE — the (day, reason, auto_invoked) PK means a
            # re-run aggregates new same-day rows on top of an existing entry.
            conn.execute(
                """
                INSERT INTO auto_hire_decisions_daily (
                    day, reason, auto_invoked, decision_count,
                    unique_callers, intent_hashes, rolled_up_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(day, reason, auto_invoked) DO UPDATE SET
                    decision_count = decision_count + excluded.decision_count,
                    unique_callers = MAX(unique_callers, excluded.unique_callers),
                    intent_hashes  = excluded.intent_hashes,
                    rolled_up_at   = excluded.rolled_up_at
                """,
                p,
            )
        conn.execute(
            "DELETE FROM auto_hire_decisions WHERE created_at < %s",
            (cutoff,),
        )
        conn.commit()
        summary["raw_rows_rolled"] = len(raw_rows)
        summary["rollup_rows_written"] = len(params)
        summary["raw_rows_deleted"] = len(raw_rows)
        return summary
    except _db.OperationalError as exc:
        # Tables missing pre-migration — silent.
        if "no such table" not in str(exc).lower():
            logger.warning("decision retention failed: %s", exc)
        return summary
    except Exception as exc:  # noqa: BLE001 — must not crash the sweeper
        logger.warning("decision retention failed: %s", exc)
        return summary
