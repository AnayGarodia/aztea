"""
disputable.py — single source of truth for "can this job be disputed?"

Lives outside `core/disputes.py` because the rate-job route also needs this
predicate, and `core/disputes.py` already imports from `payments` which
creates a tighter loop. Keep this module dependency-light.

# OWNS: the dispute-eligibility predicate.
# NOT OWNS: the dispute filing transaction (core/disputes.py), the deposit
#   formula (server/application_parts/part_010.py), or the dispute window
#   length (job.dispute_window_hours).
# INVARIANTS: never returns Ok() for a job that has never produced output
#   (completed_at must be set). The `completed_at` column is set exactly
#   once at terminal completion and never zeroed, so it is the durable
#   "did this job give the caller something to dispute" signal — more
#   reliable than `status`, which can churn post-completion (the
#   2026-05-08 power-user eval found a case where a settled job's status
#   was something other than "complete" by the time the dispute route
#   read it, even though the receipt had been issued and signed).
# DECISIONS:
#   1. Eligibility anchors on `completed_at`, not `status`. Any status that
#      is not pre-terminal (i.e. `complete`, `failed` post-completion, or
#      `stopped` from a stop_when match) is accepted as long as
#      `completed_at` is set. A job that completed-then-was-failed by a
#      downstream sweeper (e.g. verification rejection) is still a job the
#      caller paid for and may want to dispute. Pre-completion `failed`
#      jobs (completed_at IS NULL) are refunded automatically and cannot
#      be disputed — no payout to claw back. `stopped` jobs (co-pilot
#      stop_when fired) get a partial payout via pending_settlements and
#      are disputable on the same terms as `complete`.
#   2. We do NOT pre-check whether an escrow payout exists; we let the
#      clawback transaction in core/payments/trust_disputes.py decide.
#      That path is ledger-idempotent: `_lock_dispute_funds_conn` keys on
#      `(dispute_id, tx_type="deposit")` so a no-payout job produces a
#      dispute row with a zero-value escrow lock, which is correct.
#   3. Trusting decision (1) is safe because update_job_status() in
#      core/jobs/leases.py guards every status mutation with
#      `WHERE ... (%s = 0 OR completed_at IS NULL)` — once `completed_at`
#      is set, no caller can flip status to another terminal value, so the
#      sweeper-races-settlement scenario cannot produce a partial clawback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core import error_codes


@dataclass(frozen=True)
class DisputeReason:
    """First failing condition. The route maps `code` to an HTTP status."""

    code: str
    message: str
    status_code: int = 400


# Statuses that mean the job is still being worked on; disputing them is
# nonsensical (no output yet to dispute).
_PRE_TERMINAL_STATUSES = frozenset({"pending", "running", "awaiting_clarification"})


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def is_disputable(
    job: dict[str, Any],
    *,
    deadline: datetime | None,
    has_existing_dispute: bool,
    has_quality_rating: bool,
    now: datetime | None = None,
) -> DisputeReason | None:
    """Return the first reason the job can't be disputed, or ``None`` if it can.

    Caller passes pre-computed signals so this function does no I/O — it's
    pure and trivially testable. The 2026-05-08 eval flagged the strict
    `status == "complete"` inline check at part_010.py:931 as too brittle:
    a job that briefly transitioned out of "complete" between the receipt
    being signed and the dispute being filed got a 400, even though the
    caller had a signed receipt and was within the window. We anchor on
    `completed_at` (durable) and accept any terminal status whose
    `completed_at` is set.
    """
    status = str(job.get("status") or "").strip().lower()

    # 1.7.5 — check cancelled BEFORE completed_at. The 1.7.4 eval observed
    # cancelled jobs surfacing dispute.not_completed because completed_at
    # was somehow NULL despite status=cancelled (worker-dead-related race;
    # the 1.7.5 worker fix should also resolve this, but defense-in-depth
    # here ensures the right code is always emitted). Cancelled is a
    # terminal state regardless of completed_at; the message about the
    # 5¢ deposit is the actionable hint the caller needs.
    if status == "cancelled" or (
        status == "failed"
        and str(job.get("error_message") or "").startswith("Cancelled by caller")
    ):
        return DisputeReason(
            code="dispute.job_cancelled",
            message=(
                "Cancelled jobs are not disputable. The caller-initiated "
                "cancel already refunded 100%; there is no payout to claw "
                "back. Filing a dispute on a cancelled job would lock your "
                "5¢ filing deposit for nothing."
            ),
            status_code=409,
        )

    completed_at = _parse_iso(job.get("completed_at"))
    if completed_at is None:
        return DisputeReason(
            code="dispute.not_completed",
            message="Disputes can only be filed for jobs that produced output (completed_at is unset).",
        )

    if status in _PRE_TERMINAL_STATUSES:
        # `completed_at` set but status pre-terminal: shouldn't happen, but
        # if it does the job isn't actually finished. Treat as not-yet.
        return DisputeReason(
            code="dispute.not_completed",
            message=f"Job is still in '{status}'; wait for it to finish.",
        )

    if deadline is None:
        return DisputeReason(
            code="dispute.invalid_window",
            message="Dispute window could not be computed for this job.",
        )

    current = now or datetime.now(timezone.utc)
    if current > deadline:
        # Use the canonical taxonomy code (error_codes.DISPUTE_WINDOW_CLOSED)
        # so SDK error-class dispatch matches across all paths. The string-
        # match converter in server/error_handlers.py also targets this
        # value, so direct callers and HTTP callers agree.
        return DisputeReason(
            code=error_codes.DISPUTE_WINDOW_CLOSED,
            message="Dispute window has expired for this job.",
        )

    if has_existing_dispute:
        return DisputeReason(
            code="dispute.already_filed",
            message="A dispute already exists for this job.",
            status_code=409,
        )

    if has_quality_rating:
        return DisputeReason(
            code="dispute.already_rated",
            message="You already rated this job; disputes can only be filed before submitting a rating.",
            status_code=409,
        )

    return None
