# OWNS: C2 — auto-flip an agent's effective stability_tier between
#       'broken' and the spec default when its recent endpoint-error
#       rate crosses a threshold. Reads from `jobs`; writes to
#       `agents.stability_override` + `stability_flip_history`.
# NOT OWNS: scoring (auto_hire.py reads stability_override at hot path);
#       suspension policy (status='suspended' is operator-only);
#       agent registration; charge / settle (none of this touches money).
# INVARIANTS:
#   - Never writes during a job lifecycle. Sweeper-only.
#   - NULL stability_override = "honor the agent spec's stability_tier."
#     Operators can clear an override manually at any time.
#   - Every flip writes one row to stability_flip_history.
#   - Auto-flip never overrides 'banned' or 'suspended' status (those
#     are operator decisions on the agents.status column).
# DECISIONS:
#   - "Endpoint error" = job.status IN ('failed', 'cancelled') with
#     error_message LIKE '%endpoint%' OR with a 5xx attribution. The
#     thresholds (50-call window, 40% error rate) are deliberately
#     conservative; we'd rather miss a flip than dark a legitimate
#     agent. Tunable per-env via AZTEA_STABILITY_* env vars.
#   - Recovery: 20 consecutive non-error calls clears the override.
#     Asymmetric vs flip (which looks at a 50-call window) so an agent
#     can't get stuck broken on stale data.
# KNOWN DEBT:
#   - The "endpoint error" classification is heuristic; a richer
#     error-code taxonomy on the jobs table would let us distinguish
#     5xx from input-validation failures more precisely.
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from core import db as _db

logger = logging.getLogger(__name__)


# Tunables. Tightening these (lower window / higher threshold) makes
# flips rarer; loosening makes them more aggressive. Defaults err on
# the safe side — we'd rather miss a flip than dark a legitimate agent.
_DEFAULT_WINDOW = int(os.environ.get("AZTEA_STABILITY_WINDOW", "50"))
_DEFAULT_ERROR_THRESHOLD = float(
    os.environ.get("AZTEA_STABILITY_ERROR_THRESHOLD", "0.40")
)
_DEFAULT_RECOVERY_STREAK = int(
    os.environ.get("AZTEA_STABILITY_RECOVERY_STREAK", "20")
)
_DEFAULT_MIN_SAMPLE = int(os.environ.get("AZTEA_STABILITY_MIN_SAMPLE", "10"))
# /cso M4 (2026-05-28): cap how far back the sweeper looks. Without
# this, full-table scans grow linearly with history. 30 days is more
# than enough to characterize current agent health.
_DEFAULT_LOOKBACK_DAYS = int(
    os.environ.get("AZTEA_STABILITY_LOOKBACK_DAYS", "30")
)
# /cso M4: a flip to 'broken' requires errors from ≥ this many
# distinct caller owners. Without this, an agent owner could fail
# their own sock-puppet jobs to self-flip (denial-of-self that looks
# like operator action in the audit log).
_DEFAULT_MIN_DISTINCT_ERROR_CALLERS = int(
    os.environ.get("AZTEA_STABILITY_MIN_DISTINCT_ERROR_CALLERS", "3")
)
# Belt-and-suspenders M4 layer 2 (2026-05-29): recovery (clear-
# override) also requires distinct callers. Otherwise an agent owner
# who got broken-flagged could clear it by sending N self-clean
# requests from one account.
_DEFAULT_MIN_DISTINCT_RECOVERY_CALLERS = int(
    os.environ.get("AZTEA_STABILITY_MIN_DISTINCT_RECOVERY_CALLERS", "3")
)
# Belt-and-suspenders M4 layer 3 (2026-05-29): once flipped to broken,
# the agent stays broken for at least this many hours regardless of
# subsequent clean streaks. Defeats rapid-cycle exploitation where an
# attacker bursts errors, then bursts cleans, then bursts errors again.
_DEFAULT_BROKEN_MIN_HOLD_HOURS = int(
    os.environ.get("AZTEA_STABILITY_BROKEN_MIN_HOLD_HOURS", "6")
)

_BROKEN_TIER = "broken"
_ENDPOINT_ERROR_STATUSES: tuple[str, ...] = ("failed", "cancelled")
# Heuristic substrings inside error_message that we count as endpoint-side
# failures rather than input-validation rejections. Keep this list tight;
# false positives here cause unjust flips.
_ENDPOINT_ERROR_MARKERS: tuple[str, ...] = (
    "endpoint", "5xx", "502", "503", "504", "timeout", "timed out",
    "connection", "unreachable", "no response",
)


@dataclass
class _AgentStats:
    """Computed window stats for one agent."""
    agent_id: str
    total: int
    errors: int
    error_rate: float
    last_n_streak_clean: bool  # last _DEFAULT_RECOVERY_STREAK all non-error
    current_override: str | None
    distinct_error_callers: int = 0  # /cso M4 — Sybil defense on flips
    # Belt-and-suspenders M4 (2026-05-29):
    distinct_recovery_callers: int = 0  # streak-window distinct callers
    last_flip_at_iso: str | None = None  # for min-hold enforcement


@dataclass
class FlipDecision:
    """A pending change to an agent's stability_override."""
    agent_id: str
    from_tier: str | None
    to_tier: str | None  # None = clear override
    reason: str
    error_rate: float | None
    window_size: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_endpoint_error(status: str, error_message: str | None) -> bool:
    """Pure: classify a single job row as endpoint-side error or not.

    Conservative: counts only the failure shapes that almost always
    indicate the agent endpoint itself is sick (5xx, timeouts, no
    response), NOT input-validation failures the caller could fix.

    /review M1 (2026-05-28): missing error_message on a failed job is
    AMBIGUOUS, not endpoint-side. A pydantic ValidationError that gets
    serialized to a 400 with the detail in a sibling column would have
    a NULL error_message yet be caller-induced. Skipping these is the
    safer default — better to miss a flip than dark a legitimate agent
    over caller-side payload bugs.
    """
    if status not in _ENDPOINT_ERROR_STATUSES:
        return False
    if not error_message:
        return False
    lowered = error_message.lower()
    return any(marker in lowered for marker in _ENDPOINT_ERROR_MARKERS)


def _compute_stats(
    agent_id: str, window: int = _DEFAULT_WINDOW,
    recovery_streak: int = _DEFAULT_RECOVERY_STREAK,
) -> _AgentStats | None:
    """Side-effect: read the last `window` jobs for this agent.

    Returns None when there's no data to evaluate (agent never ran
    or agent_id unknown).
    """
    # /cso M4 (2026-05-28): bound the lookback so the query scales
    # with traffic rate, not lifetime history.
    from datetime import datetime, timedelta, timezone
    since_iso = (
        datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
    ).isoformat()
    try:
        with _db.get_raw_connection(_db.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT status, error_message, caller_owner_id "
                "FROM jobs "
                "WHERE agent_id = %s AND created_at >= %s "
                "ORDER BY created_at DESC LIMIT %s",
                (agent_id, since_iso, window),
            ).fetchall()
            override_row = conn.execute(
                "SELECT stability_override FROM agents WHERE agent_id = %s",
                (agent_id,),
            ).fetchone()
            # Belt-and-suspenders M4 layer 3: last broken-flip
            # timestamp for the min-hold check during recovery.
            last_flip_row = conn.execute(
                "SELECT created_at FROM stability_flip_history "
                "WHERE agent_id = %s AND to_tier = 'broken' "
                "ORDER BY created_at DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
    except Exception:  # noqa: BLE001 — never crash the sweeper
        logger.exception("stability_monitor: stats read failed for %s", agent_id)
        return None
    if not rows:
        return None
    total = len(rows)
    if total < _DEFAULT_MIN_SAMPLE:
        # Don't make decisions on tiny windows; let the agent
        # accumulate signal first.
        return None
    errors = 0
    error_callers: set[str] = set()
    for r in rows:
        if _is_endpoint_error(str(r["status"]), r["error_message"]):
            errors += 1
            owner = str(r["caller_owner_id"] or "")
            if owner:
                error_callers.add(owner)
    rate = errors / total if total else 0.0
    # Recovery streak: examine the most recent `recovery_streak` jobs
    # (already at the top of `rows` because the query orders DESC).
    streak_window = rows[:recovery_streak]
    streak_callers: set[str] = set()
    streak_clean = True
    if len(streak_window) < recovery_streak:
        streak_clean = False
    else:
        for r in streak_window:
            if _is_endpoint_error(str(r["status"]), r["error_message"]):
                streak_clean = False
                break
            owner = str(r["caller_owner_id"] or "")
            if owner:
                streak_callers.add(owner)
    override = override_row["stability_override"] if override_row else None
    last_flip_at = (
        str(last_flip_row["created_at"]) if last_flip_row else None
    )
    return _AgentStats(
        agent_id=agent_id,
        total=total,
        errors=errors,
        error_rate=rate,
        last_n_streak_clean=streak_clean,
        current_override=override,
        distinct_error_callers=len(error_callers),
        distinct_recovery_callers=len(streak_callers),
        last_flip_at_iso=last_flip_at,
    )


def _decide(
    stats: _AgentStats, threshold: float = _DEFAULT_ERROR_THRESHOLD,
) -> FlipDecision | None:
    """Pure: given window stats, return a flip decision or None.

    Cases:
    - No current override + error rate >= threshold → flip to broken.
    - Current override='broken' + clean recovery streak → clear override.
    - Otherwise → no change.
    """
    if stats.current_override is None and stats.error_rate >= threshold:
        # /cso M4 (2026-05-28): Sybil defense — require errors from
        # multiple distinct caller owners before flipping. Without
        # this, an agent owner could fail their own sock-puppet jobs
        # to self-flip (denial-of-self with system actor in the
        # audit log).
        if stats.distinct_error_callers < _DEFAULT_MIN_DISTINCT_ERROR_CALLERS:
            return None
        return FlipDecision(
            agent_id=stats.agent_id,
            from_tier=None,
            to_tier=_BROKEN_TIER,
            reason=(
                f"endpoint_error_rate {stats.error_rate:.0%} >= threshold "
                f"{threshold:.0%} over last {stats.total} jobs "
                f"({stats.distinct_error_callers} distinct callers)"
            ),
            error_rate=stats.error_rate,
            window_size=stats.total,
        )
    if stats.current_override == _BROKEN_TIER and stats.last_n_streak_clean:
        # Belt-and-suspenders M4 layer 2 (2026-05-29): require recovery
        # signals from multiple distinct callers. Without it, the
        # agent owner could self-clear a broken flip with N self-clean
        # calls from one account.
        if stats.distinct_recovery_callers < _DEFAULT_MIN_DISTINCT_RECOVERY_CALLERS:
            return None
        # Belt-and-suspenders M4 layer 3: minimum hold time. Once
        # flipped to broken, the agent stays broken for at least
        # _DEFAULT_BROKEN_MIN_HOLD_HOURS hours regardless of streak.
        # Defeats rapid burst-fail / burst-clean cycle exploits.
        if stats.last_flip_at_iso:
            try:
                from datetime import datetime, timezone, timedelta
                last_flip = datetime.fromisoformat(
                    stats.last_flip_at_iso.replace("Z", "+00:00")
                )
                if (datetime.now(timezone.utc) - last_flip
                        < timedelta(hours=_DEFAULT_BROKEN_MIN_HOLD_HOURS)):
                    return None
            except Exception:  # noqa: BLE001 — bad timestamp → be cautious, hold
                return None
        return FlipDecision(
            agent_id=stats.agent_id,
            from_tier=_BROKEN_TIER,
            to_tier=None,
            reason=(
                f"recovery_streak_clean: last {_DEFAULT_RECOVERY_STREAK} "
                f"jobs endpoint-clean from "
                f"{stats.distinct_recovery_callers} distinct callers; "
                "clearing override"
            ),
            error_rate=stats.error_rate,
            window_size=stats.total,
        )
    return None


def _apply_flip(decision: FlipDecision) -> bool:
    """Side-effect: update agents.stability_override + append audit row.

    Returns True if the change persisted, False if it was rejected
    (e.g. because the agent was concurrently suspended/banned by an
    operator). Never raises.

    Race fix (/review 2026-05-28): the prior implementation did a
    SELECT-then-UPDATE on agents.status, which left a TOCTOU window
    where an operator could suspend the agent between the two calls
    and we'd still flip the override + log a misleading audit row.
    Replaced with a single conditional UPDATE that checks status in
    the WHERE clause and uses rowcount to know whether the flip
    actually happened. Same pattern as core/payments/base.py's
    settlement race guards.
    """
    try:
        with _db.get_raw_connection(_db.DB_PATH) as conn:
            cursor = conn.execute(
                "UPDATE agents SET stability_override = %s "
                "WHERE agent_id = %s "
                "  AND status NOT IN ('banned', 'suspended')",
                (decision.to_tier, decision.agent_id),
            )
            rowcount = getattr(cursor, "rowcount", 0) or 0
            if rowcount == 0:
                # Either the agent doesn't exist or it was just
                # suspended/banned. Either way, no flip and no audit
                # row — both outcomes are honest "we did nothing".
                logger.info(
                    "stability_monitor: skip flip for %s "
                    "(agent missing or status not in {active,...})",
                    decision.agent_id,
                )
                return False
            conn.execute(
                "INSERT INTO stability_flip_history "
                "(agent_id, from_tier, to_tier, reason, error_rate, "
                " window_size, actor, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    decision.agent_id,
                    decision.from_tier,
                    decision.to_tier,
                    decision.reason,
                    decision.error_rate,
                    decision.window_size,
                    "system:stability_monitor",
                    _now_iso(),
                ),
            )
            conn.commit()
        logger.info(
            "stability_monitor: flipped %s %s → %s (%s)",
            decision.agent_id, decision.from_tier, decision.to_tier,
            decision.reason,
        )
        return True
    except Exception:  # noqa: BLE001 — sweeper-tolerant
        logger.exception(
            "stability_monitor: flip apply failed for %s",
            decision.agent_id,
        )
        return False


def _list_active_agent_ids() -> list[str]:
    """Side-effect: read all currently active agent_ids.

    Filters by status='active' so we never re-evaluate suspended /
    banned agents (operator decisions take precedence).
    """
    try:
        with _db.get_raw_connection(_db.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT agent_id FROM agents WHERE status = 'active'",
            ).fetchall()
        return [str(r["agent_id"]) for r in rows]
    except Exception:  # noqa: BLE001
        logger.exception("stability_monitor: agent list read failed")
        return []


@dataclass
class SweepResult:
    """Summary of one sweep pass — surfaced to the jobs sweeper for logging."""
    evaluated: int
    flipped_broken: int
    cleared: int
    errors: int


def run_sweep() -> SweepResult:
    """Side-effect: evaluate every active agent; apply flips + audit.

    Designed to be called from the jobs sweeper at most once per hour
    (sweeper gates the interval). Never raises; partial failures are
    counted in SweepResult.errors so the caller can log them.
    """
    result = SweepResult(evaluated=0, flipped_broken=0, cleared=0, errors=0)
    for agent_id in _list_active_agent_ids():
        stats = _compute_stats(agent_id)
        if stats is None:
            continue
        result.evaluated += 1
        decision = _decide(stats)
        if decision is None:
            continue
        if not _apply_flip(decision):
            result.errors += 1
            continue
        if decision.to_tier == _BROKEN_TIER:
            result.flipped_broken += 1
        elif decision.to_tier is None:
            result.cleared += 1
    return result


# Exposed for tests + ad-hoc operator scripts (e.g. "what does the
# monitor think about agent X right now without touching the DB?").
def evaluate_one(agent_id: str) -> tuple[_AgentStats | None, FlipDecision | None]:
    stats = _compute_stats(agent_id)
    if stats is None:
        return None, None
    return stats, _decide(stats)


__all__ = [
    "FlipDecision",
    "SweepResult",
    "evaluate_one",
    "run_sweep",
]
