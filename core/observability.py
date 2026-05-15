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
from datetime import datetime, timezone
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
