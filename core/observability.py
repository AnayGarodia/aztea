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
import os
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


@contextmanager
def time_segment(name: str) -> Generator[CallTimer, None, None]:
    """Context manager that observes one bucket of ``call_segment_seconds``.

    Used by the registry call handler and the auto-hire route to attribute
    p50/p99 to specific steps (auth, agent_lookup, embed_search, gating,
    pre_call_charge, dispatch, post_call_settle, decision_audit,
    receipt_write, work_example, output_render, outbound_http).

    Never raises. If the histogram backend errors, the timer still yields
    the elapsed value so callers that consume ``.elapsed_ms`` still work.
    """
    timer = CallTimer()
    start = time.perf_counter()
    try:
        yield timer
    finally:
        elapsed = time.perf_counter() - start
        timer.elapsed_ms = elapsed * 1000.0
        try:
            call_segment_seconds.labels(segment=name).observe(elapsed)
        except Exception as exc:  # pragma: no cover — metric path never raises
            logger.debug("observability: segment metric failed for %s: %s", name, exc)


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
    # B15 follow-up, 2026-05-19: count auto-fail events for jobs whose
    # workers never claimed. A spike on a single agent_id signals a
    # broken endpoint or a misconfigured external agent — oncall can
    # alarm on this without parsing audit logs.
    job_no_workers_claimed_total = _PCounter(
        "aztea_job_no_workers_claimed_total",
        "Jobs auto-failed by the sweeper because no worker claimed them "
        "before claim_deadline_at.",
        ["agent_id"],
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
    # Per-segment latency for the buyer-agent call path. Convention: no
    # `aztea_` prefix to match `job_duration_seconds` / `builtin_agent_calls_total`
    # (see /autoplan 2026-05-28 DX M1). Segments include auth, agent_lookup,
    # embed_search, gating, pre_call_charge, dispatch, post_call_settle,
    # decision_audit, receipt_write, work_example, output_render, outbound_http.
    # Use the `time_segment(name)` context manager to record.
    call_segment_seconds = _PHistogram(
        "call_segment_seconds",
        "Per-segment latency of the buyer-agent call path. "
        "Used to identify which step in the request handler dominates p50/p99.",
        ["segment"],
        buckets=[0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
    )
    # Outbound HTTP pool health. Saturation increments when an external-agent
    # call fails because the pool is exhausted (HTTPAdapter pool_block=False).
    # Recycles count periodic Session.close() calls done from the sweeper to
    # protect against stale keepalives behind a DNS rotation. See
    # core/outbound_session.py.
    outbound_pool_saturation_total = _PCounter(
        "outbound_pool_saturation_total",
        "Outbound HTTP requests that failed because the connection pool was full.",
        ["host"],
    )
    outbound_session_recycles_total = _PCounter(
        "outbound_session_recycles_total",
        "Outbound HTTP Session.close() recycles (DNS-rotation protection).",
    )
    # Deferred queue health. See core/deferred.py.
    deferred_processed_total = _PCounter(
        "deferred_processed_total",
        "Items drained from the deferred-write queue by the worker.",
        ["name"],
    )
    deferred_drops_total = _PCounter(
        "deferred_drops_total",
        "Items dropped from the deferred-write queue (queue full or shutdown).",
        ["name", "reason"],
    )
    deferred_drops_by_caller_total = _PCounter(
        "deferred_drops_by_caller_total",
        "Head-of-line drops attributed to a single noisy caller (caller_owner_id).",
        ["caller_owner_id"],
    )
    deferred_lag_seconds = _PHistogram(
        "deferred_lag_seconds",
        "Wall-clock seconds between enqueue and dequeue for deferred writes.",
        ["name"],
        buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
    )
    # Catalog broadcast (Postgres LISTEN/NOTIFY) reconnects. Should sit at 0
    # in steady state — a non-zero rate indicates a listener-side network
    # issue or DB restarts.
    catalog_broadcast_reconnects_total = _PCounter(
        "catalog_broadcast_reconnects_total",
        "Number of times the catalog_broadcast listener reconnected.",
    )
    # Decision-cache effectiveness.
    decision_cache_outcomes_total = _PCounter(
        "decision_cache_outcomes_total",
        "Outcome of consulting the decision cache (hit / miss / skipped / version_drift).",
        ["outcome"],
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
    job_no_workers_claimed_total = _NoopMetric()  # type: ignore[assignment]
    wallet_hold_created_total = _NoopMetric()  # type: ignore[assignment]
    wallet_hold_released_total = _NoopMetric()  # type: ignore[assignment]
    wallet_hold_clawed_total = _NoopMetric()  # type: ignore[assignment]
    payout_curve_clawback_skipped_total = _NoopMetric()  # type: ignore[assignment]
    route_decisions_total = _NoopMetric()  # type: ignore[assignment]
    route_latency_seconds = _NoopMetric()  # type: ignore[assignment]
    call_segment_seconds = _NoopMetric()  # type: ignore[assignment]
    outbound_pool_saturation_total = _NoopMetric()  # type: ignore[assignment]
    outbound_session_recycles_total = _NoopMetric()  # type: ignore[assignment]
    deferred_processed_total = _NoopMetric()  # type: ignore[assignment]
    deferred_drops_total = _NoopMetric()  # type: ignore[assignment]
    deferred_drops_by_caller_total = _NoopMetric()  # type: ignore[assignment]
    deferred_lag_seconds = _NoopMetric()  # type: ignore[assignment]
    catalog_broadcast_reconnects_total = _NoopMetric()  # type: ignore[assignment]
    decision_cache_outcomes_total = _NoopMetric()  # type: ignore[assignment]


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


# Anonymous playground rows have no billing tie — they accumulate purely
# as an abuse-investigation aid. 30 days is long enough for incident
# response (the kill-switch and judge dashboards both look back ≤ 14d)
# and short enough that an adversary can't build a multi-month fingerprint
# corpus of probe hashes. Authenticated rows (``caller_owner_id IS NOT NULL``)
# are kept indefinitely — those tie to a paying customer's audit trail.
_HOSTED_EXECUTION_LOG_RETENTION_DAYS = 30


def _hosted_execution_log_cutoff(now_utc: datetime | None = None) -> str:
    """Pure: ISO timestamp ``_HOSTED_EXECUTION_LOG_RETENTION_DAYS`` ago."""
    base = now_utc or datetime.now(timezone.utc)
    cutoff = base - timedelta(days=_HOSTED_EXECUTION_LOG_RETENTION_DAYS)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


# Plan B Phase 3b (2026-05-27): continuous endpoint health probe.
#
# An agent registered weeks ago can rot — DNS expires, the seller's Fly
# app shuts down, a key rotates. Without a continuous probe, the rot is
# only detected at call time, after the buyer paid pre-call charge. The
# sweeper hits each registered http(s):// endpoint at a low cadence,
# updates last_health_status, and after AZTEA_HEALTH_SUSPEND_THRESHOLD
# consecutive failures transitions the agent to suspended so it stops
# matching for new calls.
_HEALTH_SUSPEND_THRESHOLD_DEFAULT = 3
_HEALTH_PROBE_TIMEOUT_SECONDS = 5
_HEALTH_BATCH_SIZE = 50  # how many agents to probe per sweeper pass


def _health_suspend_threshold() -> int:
    """Pure: env-tunable threshold for consecutive-failure suspension."""
    raw = os.environ.get("AZTEA_HEALTH_SUSPEND_THRESHOLD", "").strip()
    try:
        value = int(raw) if raw else _HEALTH_SUSPEND_THRESHOLD_DEFAULT
    except ValueError:
        value = _HEALTH_SUSPEND_THRESHOLD_DEFAULT
    return max(1, value)


def _probe_endpoint_health(endpoint_url: str, signing_secret: str | None) -> bool:
    """Side-effect: send one lightweight health probe. True on 2xx/3xx/4xx (alive)."""
    try:
        import requests
    except Exception:  # noqa: BLE001
        return False
    # 2026-05-27 audit fix (SSRF): re-validate the outbound URL on every
    # probe. The registration-time check resolved the host then; DNS
    # rebinding between register and any later sweep would otherwise let
    # the sweeper hit a private IP / cloud metadata via Aztea's egress
    # identity with a valid HMAC signature. validate_outbound_url
    # re-resolves the hostname and refuses if it points anywhere private.
    try:
        from core import url_security as _url_security
        safe_url = _url_security.validate_outbound_url(endpoint_url, "endpoint_url")
    except ValueError:
        # An endpoint that newly resolves to a private IP is dead from
        # Aztea's perspective. Count it as failed so the streak progresses
        # toward suspension — better to suspend a rebinding endpoint than
        # to silently keep paying for a half-broken listing.
        return False
    except Exception:  # noqa: BLE001 — url_security unimportable in test stubs
        safe_url = endpoint_url
    body_bytes = b'{"_aztea_health":true}'
    headers = {"Content-Type": "application/json", "User-Agent": "Aztea-HealthProbe/1.0"}
    if signing_secret:
        try:
            from core import crypto as _crypto
            from datetime import datetime as _dt, timezone as _tz
            ts = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            headers["X-Aztea-Signature"] = _crypto.sign_endpoint_request(
                body_bytes, signing_secret, ts,
            )
            headers["X-Aztea-Timestamp"] = ts
        except Exception:  # noqa: BLE001
            pass
    try:
        resp = requests.post(
            safe_url, data=body_bytes, headers=headers,
            timeout=_HEALTH_PROBE_TIMEOUT_SECONDS, allow_redirects=False,
        )
    except Exception:  # noqa: BLE001 — network error / DNS / timeout / SSL
        return False
    status = getattr(resp, "status_code", 0)
    # 5xx = dead. Anything 1xx/2xx/3xx/4xx = endpoint is at least answering;
    # 4xx is a structured rejection, which is healthier than no response.
    return 100 <= status < 500


def run_endpoint_health_sweep() -> dict[str, int]:
    """Side-effect: probe up to N registered http(s):// agents and update health state.

    Returns a small summary dict for log emission. Never raises.

    Idempotent: running twice in quick succession is fine; the probe is
    cheap and the batching loop bounds per-pass cost. Suspended agents
    are skipped (no point probing a dead listing). Aztea-hosted
    (internal://, skill://) endpoints are skipped — they're served
    in-process and can't fail the way an external endpoint can.
    """
    summary = {"probed": 0, "healthy": 0, "failed": 0, "suspended": 0}
    threshold = _health_suspend_threshold()
    try:
        conn: _db.DbConnection = _db.get_raw_connection(_db.DB_PATH)
        rows = conn.execute(
            """
            SELECT agent_id, endpoint_url, endpoint_signing_secret,
                   consecutive_health_failures
            FROM agents
            WHERE status = 'active'
              AND endpoint_url IS NOT NULL
              AND endpoint_url NOT LIKE 'internal://%'
              AND endpoint_url NOT LIKE 'skill://%'
            ORDER BY COALESCE(last_health_check_at, '') ASC
            LIMIT %s
            """,
            (_HEALTH_BATCH_SIZE,),
        ).fetchall()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for row in rows:
            try:
                aid = row["agent_id"]
                endpoint_url = row["endpoint_url"]
                signing_secret = row["endpoint_signing_secret"]
                streak = int(row["consecutive_health_failures"] or 0)
            except (TypeError, KeyError):
                aid, endpoint_url, signing_secret, streak = row[0], row[1], row[2], int(row[3] or 0)
            alive = _probe_endpoint_health(endpoint_url, signing_secret)
            summary["probed"] += 1
            if alive:
                summary["healthy"] += 1
                conn.execute(
                    """
                    UPDATE agents
                    SET last_health_status = 'ok',
                        last_health_check_at = %s,
                        consecutive_health_failures = 0
                    WHERE agent_id = %s
                    """,
                    (now_iso, aid),
                )
            else:
                summary["failed"] += 1
                new_streak = streak + 1
                # 2026-05-27 audit fix: optimistic-concurrency guard. The
                # SELECT/UPDATE pair has no row lock, so two sweepers
                # running concurrently could both read streak=2, both
                # probe, both write streak=3, both flip status=suspended.
                # The WHERE clause now requires the streak to still
                # match what we read — racing UPDATEs detect via
                # rowcount=0 and skip silently. Idempotent outcome,
                # no double-increment, no race-induced suspension.
                if new_streak >= threshold:
                    conn.execute(
                        """
                        UPDATE agents
                        SET last_health_status = 'failed',
                            last_health_check_at = %s,
                            consecutive_health_failures = %s,
                            status = 'suspended',
                            suspension_reason = 'health_check_failed'
                        WHERE agent_id = %s
                          AND status = 'active'
                          AND consecutive_health_failures = %s
                        """,
                        (now_iso, new_streak, aid, streak),
                    )
                    summary["suspended"] += 1
                    logger.warning(
                        "Agent %s suspended after %d consecutive health failures (endpoint %s)",
                        aid, new_streak, endpoint_url,
                    )
                else:
                    conn.execute(
                        """
                        UPDATE agents
                        SET last_health_status = 'failed',
                            last_health_check_at = %s,
                            consecutive_health_failures = %s
                        WHERE agent_id = %s
                          AND consecutive_health_failures = %s
                        """,
                        (now_iso, new_streak, aid, streak),
                    )
        conn.commit()
        return summary
    except _db.OperationalError as exc:
        if "no such table" not in str(exc).lower() and "no such column" not in str(exc).lower():
            logger.warning("endpoint health sweep failed: %s", exc)
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.warning("endpoint health sweep failed: %s", exc)
        return summary


def run_hosted_execution_log_retention() -> dict[str, int]:
    """Side-effect: delete anonymous playground rows older than the cutoff.

    Mirrors ``run_decision_retention`` in structure. Only deletes rows
    where ``caller_owner_id IS NULL`` so authenticated invocations
    (billing-relevant) stay forever. Never raises.
    """
    summary = {"rows_deleted": 0}
    try:
        conn: _db.DbConnection = _db.get_raw_connection(_db.DB_PATH)
        cutoff = _hosted_execution_log_cutoff()
        result = conn.execute(
            """
            DELETE FROM hosted_execution_log
            WHERE caller_owner_id IS NULL
              AND created_at < %s
            """,
            (cutoff,),
        )
        conn.commit()
        # rowcount is the standard cursor attribute; both SQLite and psycopg2
        # populate it after DELETE.
        deleted = getattr(result, "rowcount", 0) or 0
        summary["rows_deleted"] = int(deleted)
        return summary
    except _db.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            logger.warning("hosted_execution_log retention failed: %s", exc)
        return summary
    except Exception as exc:  # noqa: BLE001 — must not crash the sweeper
        logger.warning("hosted_execution_log retention failed: %s", exc)
        return summary
