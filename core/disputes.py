# OWNS: dispute state transitions, judgment recording, bilateral caller-rating reads
# NOT OWNS: money movements (trust_disputes.py wraps these), caller_ratings table (reputation.py)
#
# INVARIANTS:
# - dispute insert + escrow clawback MUST happen in ONE transaction — pass an open conn to create_dispute
# - at most one dispute per job — enforced by UNIQUE index on disputes.job_id
# - only a party to the job (caller or agent owner) may file — this check is authoritative
# - never declare or write caller_ratings directly; use reputation.py helpers
#
# DECISIONS:
# - create_dispute accepts an optional conn so the server shard can wrap it with escrow clawback
#   in one transaction. If no conn is passed, a new transaction opens internally (safe when
#   no money movement is needed — e.g. test disputes). This two-mode design was intentional.
# - status machine is append-only via judgment rows, not in-place status updates, to preserve audit trail
#
# Status machine: pending → judging → consensus (2 LLM judges agree) → final
#                                   → tied → final (admin tie-break)
#                 any status → appealed → final

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone

_LOG = logging.getLogger(__name__)

from core import db as _db
from core.functional import Err, Ok, Result

DB_PATH = _db.DB_PATH
_local = _db._local

DISPUTE_SIDES = {"caller", "agent"}

# F4 (red-team 2026-05-19): the verification-rejection flow
# (_ensure_output_rejection_dispute in part_005.py) creates a dispute
# whose target job is FAILED rather than COMPLETED — its eligibility
# was already validated by the verification window logic. Other call
# sites (route handlers, MCP) MUST go through the write-path check.
# A ContextVar is the cleanest signal because the bypass is per-request,
# not a global; it never leaks across requests.
_ALLOW_PRE_TERMINAL_DISPUTE_CREATE: contextvars.ContextVar[bool] = (
    contextvars.ContextVar("aztea.disputes.allow_pre_terminal", default=False)
)


def allow_pre_terminal_dispute_create() -> contextvars.Token:
    """Side-effect: temporarily allow create_dispute on a non-completed job.

    Used ONLY by the verification-rejection internal flow which has
    already established eligibility through the output-verification
    window. Caller MUST hold the returned token and pass it to
    ``reset_pre_terminal_bypass`` in a try/finally to scope the bypass.
    """
    return _ALLOW_PRE_TERMINAL_DISPUTE_CREATE.set(True)


def reset_pre_terminal_bypass(token: contextvars.Token) -> None:
    """Side-effect: restore the bypass flag to its prior value."""
    _ALLOW_PRE_TERMINAL_DISPUTE_CREATE.reset(token)
DISPUTE_STATUSES = {
    "pending",
    # 2026-05-18 (D3): the dispute waits in awaiting_operator until the
    # agent operator submits a defense or the response deadline expires.
    # The sweeper auto-advances expired rows into 'judging'. The CHECK
    # constraint on the column was set at table-creation time and isn't
    # ALTERable in SQLite without a table rewrite — enforcement of the
    # status set lives in this set + create/transition helpers below.
    "awaiting_operator",
    "judging",
    "consensus",
    "tied",
    "resolved",
    "appealed",
    "final",
}
DISPUTE_OUTCOMES = {"caller_wins", "agent_wins", "split", "void"}
JUDGE_KINDS = {"llm_primary", "llm_secondary", "human_admin"}
# Default response window for the operator before the dispute auto-
# advances to judging. Tunable via DISPUTE_OPERATOR_RESPONSE_HOURS env.
DEFAULT_OPERATOR_RESPONSE_HOURS = 24


def _resolved_db_path() -> str:
    """Prefer ``core.disputes.DB_PATH`` so isolated tests can monkeypatch it."""
    module = sys.modules.get("core.disputes")
    if module is not None:
        candidate = getattr(module, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return DB_PATH


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(_resolved_db_path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_disputes_db() -> None:
    """Create dispute-related tables (disputes, dispute_judgments) if they do not exist. Idempotent."""
    if _db.IS_POSTGRES:
        return
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS disputes (
                dispute_id           TEXT PRIMARY KEY,
                job_id               TEXT NOT NULL REFERENCES jobs(job_id),
                filed_by_owner_id    TEXT NOT NULL,
                side                 TEXT NOT NULL CHECK(side IN ('caller','agent')),
                reason               TEXT NOT NULL,
                evidence             TEXT,
                filing_deposit_cents INTEGER NOT NULL DEFAULT 0 CHECK(filing_deposit_cents >= 0),
                status               TEXT NOT NULL CHECK(status IN ('pending','judging','consensus','tied','resolved','appealed','final')),
                outcome              TEXT CHECK(outcome IN ('caller_wins','agent_wins','split','void')),
                split_caller_cents   INTEGER,
                split_agent_cents    INTEGER,
                filed_at             TEXT NOT NULL,
                resolved_at          TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_judgments (
                judgment_id     TEXT PRIMARY KEY,
                dispute_id      TEXT NOT NULL,
                judge_kind      TEXT NOT NULL,
                verdict         TEXT NOT NULL,
                reasoning       TEXT NOT NULL,
                model           TEXT,
                admin_user_id   TEXT,
                created_at      TEXT NOT NULL
            )
            """
        )
        # caller_ratings is defined in reputation.py; that table must be initialized first
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_disputes_job_unique ON disputes(job_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_disputes_status_filed ON disputes(status, filed_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_disputes_filer_filed ON disputes(filed_by_owner_id, filed_at DESC)"
        )
        # Add filing_deposit_cents if missing — idempotent via duplicate-column detection.
        # PRAGMA table_info is SQLite-only; we use a direct ALTER and ignore the error instead.
        try:
            conn.execute(
                "ALTER TABLE disputes ADD COLUMN filing_deposit_cents INTEGER NOT NULL DEFAULT 0"
            )
        except _db.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        # 2026-05-19 (B14): pin the resolution_by deadline at filing
        # time. Mirrored in migration 0061; also added here so tests
        # using init_db() pick up the column without re-running the
        # migration runner.
        try:
            conn.execute("ALTER TABLE disputes ADD COLUMN resolution_deadline_at TEXT")
        except _db.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dispute_judgments_dispute_created ON dispute_judgments(dispute_id, created_at ASC)"
        )


def _row_to_dispute(row: dict) -> dict:
    data = dict(row)
    for field in ("filing_deposit_cents", "split_caller_cents", "split_agent_cents"):
        value = data.get(field)
        data[field] = int(value) if value is not None else None
    # 2026-05-19 (B13): surface degraded_mode from the audit log so the
    # dispute_status response tells the caller that the two-judge
    # guarantee collapsed to one judge + deterministic tiebreaker. Pure
    # read of audit_log; the actual write happened at fallback time.
    audit_raw = data.get("audit_log")
    degraded_event: dict | None = None
    if isinstance(audit_raw, str) and audit_raw:
        try:
            log = json.loads(audit_raw)
        except (json.JSONDecodeError, TypeError):
            log = []
        if isinstance(log, list):
            for entry in log:
                if isinstance(entry, dict) and entry.get("event") == "secondary_judge_fallback":
                    degraded_event = entry
                    break
    if degraded_event is not None:
        data["degraded_mode"] = True
        data["degraded_reason"] = degraded_event.get(
            "reason", "secondary_judge_llm_unavailable"
        )
    else:
        data["degraded_mode"] = False
    # 2026-05-19 (B14): resolution_by is now pinned at INSERT via
    # resolution_deadline_at. Read it as resolution_by for the response so
    # legacy callers see the same field name with a stable value.
    if "resolution_deadline_at" in data and data.get("resolution_deadline_at"):
        data["resolution_by"] = data["resolution_deadline_at"]
    return data


def _validate_side(side: str) -> str:
    normalized = str(side or "").strip().lower()
    if normalized not in DISPUTE_SIDES:
        raise ValueError("side must be either 'caller' or 'agent'.")
    return normalized


def _validate_side_result(side: str) -> "Result[str, str]":
    try:
        return Ok(_validate_side(side))
    except ValueError as exc:
        return Err(str(exc))


def _validate_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized not in DISPUTE_STATUSES:
        raise ValueError("invalid dispute status")
    return normalized


def _validate_status_result(status: str) -> "Result[str, str]":
    try:
        return Ok(_validate_status(status))
    except ValueError as exc:
        return Err(str(exc))


def _validate_outcome(outcome: str | None) -> str | None:
    if outcome is None:
        return None
    normalized = str(outcome or "").strip().lower()
    if normalized not in DISPUTE_OUTCOMES:
        raise ValueError("invalid dispute outcome")
    return normalized


def _validate_outcome_result(outcome: str | None) -> "Result[str | None, str]":
    try:
        return Ok(_validate_outcome(outcome))
    except ValueError as exc:
        return Err(str(exc))


def _validate_split(
    outcome: str | None, split_caller_cents: int | None, split_agent_cents: int | None
) -> tuple[int | None, int | None]:
    if outcome != "split":
        return None, None
    if split_caller_cents is None or split_agent_cents is None:
        raise ValueError(
            "split outcomes require split_caller_cents and split_agent_cents."
        )
    if split_caller_cents < 0 or split_agent_cents < 0:
        raise ValueError("split amounts must be non-negative.")
    return int(split_caller_cents), int(split_agent_cents)


def _validate_split_result(
    outcome: str | None, split_caller_cents: int | None, split_agent_cents: int | None
) -> "Result[tuple[int | None, int | None], str]":
    try:
        return Ok(_validate_split(outcome, split_caller_cents, split_agent_cents))
    except ValueError as exc:
        return Err(str(exc))


def _operator_response_enabled() -> bool:
    """Pure: feature-flag the operator-response slot. Default off until UI ships."""
    import os
    return os.environ.get("AZTEA_DISPUTE_OPERATOR_RESPONSE_ENABLED", "0").lower() in {
        "1", "true", "yes", "on",
    }


def _operator_response_hours() -> int:
    """Pure: env-tunable response window."""
    import os
    try:
        return max(1, int(os.environ.get(
            "AZTEA_DISPUTE_OPERATOR_RESPONSE_HOURS",
            str(DEFAULT_OPERATOR_RESPONSE_HOURS),
        )))
    except (TypeError, ValueError):
        return DEFAULT_OPERATOR_RESPONSE_HOURS


# B14, 2026-05-19: pin resolution_by deadline at filing time. Default 48h
# from filed_at; env-tunable for tighter SLAs on premium tiers.
DEFAULT_DISPUTE_RESOLUTION_HOURS = 48


def _dispute_resolution_window_hours() -> int:
    """Pure: env-tunable resolution window for the resolution_by deadline.

    Used only at INSERT (B14 — the deadline is pinned and never updated).
    The default tracks the documented "5-30 minutes typical, 48h SLA"
    contract from the dispute description.
    """
    import os
    try:
        return max(1, int(os.environ.get(
            "AZTEA_DISPUTE_RESOLUTION_WINDOW_HOURS",
            str(DEFAULT_DISPUTE_RESOLUTION_HOURS),
        )))
    except (TypeError, ValueError):
        return DEFAULT_DISPUTE_RESOLUTION_HOURS


def create_dispute(
    *,
    job_id: str,
    filed_by_owner_id: str,
    side: str,
    reason: str,
    evidence: str | None = None,
    filing_deposit_cents: int = 0,
    conn: _db.DbConnection | None = None,
) -> dict:
    """Insert a new dispute row for a completed job.

    If ``conn`` is provided the INSERT is executed on that connection so the
    caller can wrap it with the escrow clawback in a single atomic transaction.
    If ``conn`` is None a new connection + transaction is used (safe for
    dispute creation without a money movement, e.g. in tests).

    Raises ``ValueError`` if the job does not exist, if reason is blank, or
    if ``filed_by_owner_id`` is not a party to the job.
    Raises ``PermissionError`` if the filer is not the caller or agent owner.

    2026-05-18 (D3): when ``AZTEA_DISPUTE_OPERATOR_RESPONSE_ENABLED=1`` and
    the dispute was filed by the caller side, the row starts in
    ``awaiting_operator`` with a deadline. The operator (agent side) gets
    a window to defend; the sweeper auto-advances expired rows to
    ``judging``. Default off until the operator-response UI ships.
    """
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise ValueError("reason must be a non-empty string.")

    # Enforce that the filer is actually a party to the job, regardless of call site.
    # When a conn is provided we must use it directly — opening a second `with _conn()`
    # would get the same thread-local connection and its __exit__ would commit the
    # caller's in-progress BEGIN IMMEDIATE, breaking atomicity.
    def _fetch_job_parties(c: _db.DbConnection) -> dict | None:
        # F4 (red-team 2026-05-19): also fetch completed_at and status so
        # the write-path can enforce the same eligibility predicate as
        # the read-time `is_disputable` annotation. Pre-fix, the route
        # handler called `is_disputable` (read-time only) — a race or
        # alternate dispute creation path could file a dispute on a
        # PENDING / RUNNING job, locking the filing deposit during the
        # judge run with no payout to claw back.
        return c.execute(
            "SELECT caller_owner_id, agent_owner_id, completed_at, status, "
            "error_message FROM jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()

    if conn is not None:
        job_row = _fetch_job_parties(conn)
    else:
        with _conn() as _check_conn:
            job_row = _fetch_job_parties(_check_conn)
    if job_row is None:
        raise ValueError(f"Job '{job_id}' not found.")
    parties = {job_row["caller_owner_id"], job_row["agent_owner_id"]}
    if str(filed_by_owner_id).strip() not in parties:
        raise PermissionError("Only a party to the job may file a dispute.")

    # F4 — write-path eligibility check. Mirrors core.jobs.disputable's
    # core predicates (completed_at is set, status not cancelled). Skipped
    # only when ``_ALLOW_PRE_TERMINAL_DISPUTE_CREATE`` is True, which is
    # set by the operator-rejection path (_ensure_output_rejection_dispute)
    # that already validated eligibility via the verification flow.
    if not _ALLOW_PRE_TERMINAL_DISPUTE_CREATE.get(False):
        job_status = str(job_row.get("status") or "").strip().lower()
        job_completed_at = job_row.get("completed_at")
        # Cancelled / explicitly-cancelled-via-failed never disputable.
        if job_status == "cancelled" or (
            job_status == "failed"
            and str(job_row.get("error_message") or "")
            .startswith("Cancelled by caller")
        ):
            raise ValueError("dispute.job_cancelled: cancelled jobs are not disputable")
        # The job must have an output to dispute. completed_at is set
        # exactly once and never zeroed (see core.jobs.disputable.py
        # rationale). Pending / running / awaiting_clarification / claimed
        # all fail this check.
        if not job_completed_at:
            raise ValueError(
                "dispute.not_completed: disputes can only be filed for jobs "
                "that produced output (completed_at is unset)"
            )

    dispute_id = str(uuid.uuid4())
    now = _now()
    normalized_filing_deposit = int(filing_deposit_cents)
    if normalized_filing_deposit < 0:
        raise ValueError("filing_deposit_cents must be non-negative.")
    normalized_side = _validate_side(side)
    # Operator-response slot only applies to caller-side filings — when the
    # operator already filed, they don't need a chance to defend themselves.
    use_operator_window = (
        _operator_response_enabled() and normalized_side == "caller"
    )
    if use_operator_window:
        initial_status = "awaiting_operator"
        deadline = (
            datetime.now(timezone.utc)
            + timedelta(hours=_operator_response_hours())
        ).isoformat()
    else:
        initial_status = "pending"
        deadline = None
    # B14, 2026-05-19: pin resolution_by at filing time. Stored as
    # resolution_deadline_at; read back as resolution_by in the response.
    # Never UPDATE'd anywhere — judges run against the dispute without
    # touching the deadline, so what the caller sees at filing matches
    # what they see hours later.
    resolution_deadline_iso = (
        datetime.now(timezone.utc)
        + timedelta(hours=_dispute_resolution_window_hours())
    ).isoformat()
    params = (
        dispute_id,
        job_id,
        str(filed_by_owner_id).strip(),
        normalized_side,
        normalized_reason,
        str(evidence).strip() if evidence else None,
        normalized_filing_deposit,
        initial_status,
        now,
        deadline,
        resolution_deadline_iso,
    )

    def _insert(created_conn: _db.DbConnection) -> dict:
        created_conn.execute(
            """
            INSERT INTO disputes
                (dispute_id, job_id, filed_by_owner_id, side, reason, evidence,
                 filing_deposit_cents, status, filed_at,
                 operator_response_deadline, resolution_deadline_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            params,
        )
        row = created_conn.execute(
            "SELECT * FROM disputes WHERE dispute_id = %s",
            (dispute_id,),
        ).fetchone()
        return _row_to_dispute(row) if row else {}

    if conn is not None:
        return _insert(conn)
    with _conn() as db_conn:
        return _insert(db_conn)


def get_dispute(dispute_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM disputes WHERE dispute_id = %s",
            (dispute_id,),
        ).fetchone()
    return _row_to_dispute(row) if row else None


def get_dispute_by_job(job_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM disputes WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    return _row_to_dispute(row) if row else None


def has_dispute_for_job(job_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM disputes WHERE job_id = %s LIMIT 1",
            (job_id,),
        ).fetchone()
    return row is not None


def get_dispute_for_job(job_id: str) -> dict | None:
    """Return the (single) dispute attached to a job, or None.

    The uniqueness constraint on disputes(job_id) means at most one row
    is ever returned. Used by the operator-response endpoint to look up
    the dispute without a status-scan.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM disputes WHERE job_id = %s",
            (str(job_id).strip(),),
        ).fetchone()
    return _row_to_dispute(row) if row else None


def list_disputes(*, status: str | None = None, limit: int = 100) -> list[dict]:
    """Return a paginated list of disputes, optionally filtered by ``status``. Max 500 rows."""
    capped = min(max(1, int(limit)), 500)
    with _conn() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT * FROM disputes
                WHERE status = %s
                ORDER BY filed_at DESC
                LIMIT %s
                """,
                (_validate_status(status), capped),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM disputes
                ORDER BY filed_at DESC
                LIMIT %s
                """,
                (capped,),
            ).fetchall()
    return [_row_to_dispute(row) for row in rows]


def set_dispute_status(dispute_id: str, status: str) -> dict | None:
    """Update the ``status`` field of a dispute. Returns the updated dispute or None if not found."""
    normalized_status = _validate_status(status)
    with _conn() as conn:
        conn.execute(
            """
            UPDATE disputes
            SET status = %s
            WHERE dispute_id = %s
            """,
            (normalized_status, dispute_id),
        )
    return get_dispute(dispute_id)


def record_operator_response(
    dispute_id: str,
    *,
    operator_owner_id: str,
    response_text: str,
) -> dict | None:
    """Record the agent operator's defense and advance the dispute to 'judging'.

    Returns the updated dispute row, or None if the dispute doesn't exist.
    Raises:
      * ``PermissionError`` if ``operator_owner_id`` is not the agent owner.
      * ``ValueError`` if the dispute isn't in ``awaiting_operator`` or
        the response_text is blank, or the operator-response window has
        already expired.
    """
    text = str(response_text or "").strip()
    if not text:
        raise ValueError("response_text must be a non-empty string.")
    if len(text) > 10_000:
        raise ValueError("response_text exceeds the 10 000-char limit.")
    dispute = get_dispute(dispute_id)
    if dispute is None:
        return None
    current_status = str(dispute.get("status") or "").strip().lower()
    if current_status != "awaiting_operator":
        raise ValueError(
            f"Dispute is in status {current_status!r}; operator responses are "
            "only accepted while the dispute is 'awaiting_operator'."
        )
    job_id = str(dispute.get("job_id") or "")
    with _conn() as conn:
        job_row = conn.execute(
            "SELECT agent_owner_id FROM jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    if job_row is None:
        raise ValueError(f"Job '{job_id}' not found for dispute.")
    agent_owner = str(job_row["agent_owner_id"] or "").strip()
    if agent_owner != str(operator_owner_id).strip():
        raise PermissionError(
            "Only the agent operator may record an operator response."
        )
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE disputes
            SET status = 'judging',
                operator_response_text = %s,
                operator_response_at = %s
            WHERE dispute_id = %s
            """,
            (text, now, dispute_id),
        )
    return get_dispute(dispute_id)


def expire_operator_response_windows(*, limit: int = 100) -> int:
    """Side-effect: advance ``awaiting_operator`` disputes whose deadline has passed.

    Called by the background sweeper. Returns the number of disputes
    advanced. The operator's silence is treated as an implicit waiver of
    response — the LLM judges then see only the caller's evidence.
    """
    now = _now()
    capped = min(max(1, int(limit)), 500)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT dispute_id FROM disputes
            WHERE status = 'awaiting_operator'
              AND operator_response_deadline IS NOT NULL
              AND operator_response_deadline < %s
            ORDER BY operator_response_deadline ASC
            LIMIT %s
            """,
            (now, capped),
        ).fetchall()
        ids = [str(r["dispute_id"]) for r in (rows or [])]
        if not ids:
            return 0
        for did in ids:
            conn.execute(
                """
                UPDATE disputes
                SET status = 'judging'
                WHERE dispute_id = %s AND status = 'awaiting_operator'
                """,
                (did,),
            )
    return len(ids)


def set_dispute_tied(dispute_id: str) -> dict | None:
    """Transition to 'tied' and stamp tied_since only on the first transition."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE disputes
            SET status = 'tied',
                tied_since = CASE WHEN tied_since IS NULL THEN %s ELSE tied_since END
            WHERE dispute_id = %s
            """,
            (now, dispute_id),
        )
    return get_dispute(dispute_id)


def get_stale_tied_disputes(older_than_hours: int = 48, limit: int = 100) -> list[dict]:
    """Return tied disputes whose tied_since is older than the given threshold."""
    from datetime import timedelta

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    ).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM disputes
            WHERE status = 'tied'
              AND tied_since IS NOT NULL
              AND tied_since <= %s
            ORDER BY tied_since ASC
            LIMIT %s
            """,
            (cutoff, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_row_to_dispute(row) for row in rows]


def set_dispute_consensus(dispute_id: str, outcome: str) -> dict | None:
    """Record that two judge votes agree on ``outcome``; transitions status to 'consensus'."""
    normalized_outcome = _validate_outcome(outcome)
    with _conn() as conn:
        conn.execute(
            """
            UPDATE disputes
            SET status = 'consensus', outcome = %s
            WHERE dispute_id = %s
            """,
            (normalized_outcome, dispute_id),
        )
    return get_dispute(dispute_id)


def finalize_dispute(
    dispute_id: str,
    *,
    status: str,
    outcome: str,
    split_caller_cents: int | None = None,
    split_agent_cents: int | None = None,
) -> dict | None:
    """Mark a dispute as final after admin ruling or consensus; records outcome and resolved_at timestamp.

    For 'split' outcomes, ``split_caller_cents`` and ``split_agent_cents`` are required.
    Returns the updated dispute dict or None if not found.
    """
    normalized_status = _validate_status(status)
    normalized_outcome = _validate_outcome(outcome)
    caller_split, agent_split = _validate_split(
        normalized_outcome, split_caller_cents, split_agent_cents
    )
    resolved_at = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE disputes
            SET status = %s,
                outcome = %s,
                split_caller_cents = %s,
                split_agent_cents = %s,
                resolved_at = %s
            WHERE dispute_id = %s
            """,
            (
                normalized_status,
                normalized_outcome,
                caller_split,
                agent_split,
                resolved_at,
                dispute_id,
            ),
        )
    return get_dispute(dispute_id)


def record_judgment(
    dispute_id: str,
    *,
    judge_kind: str,
    verdict: str,
    reasoning: str,
    model: str | None = None,
    admin_user_id: str | None = None,
) -> dict:
    """Persist a single LLM or admin judge vote for a dispute and return the row.

    2026-05-18 (D2): idempotent on the unique (dispute_id, judge_kind) index
    added in migration 0057. A retry — including a brief two-leader overlap
    during a lease handoff, or a re-run of a 'judging' dispute whose first
    pass died mid-flight — finds the existing row and returns it instead
    of double-voting.
    """
    normalized_kind = str(judge_kind or "").strip().lower()
    if normalized_kind not in JUDGE_KINDS:
        raise ValueError("invalid judge_kind")
    normalized_verdict = _validate_outcome(verdict)
    normalized_reasoning = str(reasoning or "").strip()
    if not normalized_reasoning:
        raise ValueError("reasoning must be a non-empty string.")

    judgment = {
        "judgment_id": str(uuid.uuid4()),
        "dispute_id": dispute_id,
        "judge_kind": normalized_kind,
        "verdict": normalized_verdict,
        "reasoning": normalized_reasoning,
        "model": str(model).strip() if model else None,
        "admin_user_id": str(admin_user_id).strip() if admin_user_id else None,
        "created_at": _now(),
    }

    with _conn() as conn:
        try:
            conn.execute(
                """
                INSERT INTO dispute_judgments
                    (judgment_id, dispute_id, judge_kind, verdict, reasoning, model, admin_user_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    judgment["judgment_id"],
                    judgment["dispute_id"],
                    judgment["judge_kind"],
                    judgment["verdict"],
                    judgment["reasoning"],
                    judgment["model"],
                    judgment["admin_user_id"],
                    judgment["created_at"],
                ),
            )
        except _db.IntegrityError:
            existing = conn.execute(
                "SELECT judgment_id, dispute_id, judge_kind, verdict, reasoning, "
                "model, admin_user_id, created_at FROM dispute_judgments "
                "WHERE dispute_id = %s AND judge_kind = %s",
                (dispute_id, normalized_kind),
            ).fetchone()
            if existing is None:
                # Constraint fired without a visible row — extremely rare
                # but worth surfacing rather than silently lying.
                raise
            return {k: existing[k] for k in existing.keys()}
    return judgment


def append_audit_event(
    dispute_id: str, event: str, actor: str | None = None, extra: dict | None = None
) -> None:
    """Append a structured entry to disputes.audit_log (JSON array).

    Safe to call even if the column doesn't exist yet (migration guard).
    Never raises — audit failures must not block the main transaction.
    """
    now = _now()
    entry: dict = {"event": event, "at": now}
    if actor:
        entry["actor"] = actor
    if extra:
        entry.update(extra)
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT audit_log FROM disputes WHERE dispute_id = %s",
                (dispute_id,),
            ).fetchone()
            if row is None:
                return
            existing = row["audit_log"] if isinstance(row["audit_log"], str) else "[]"
            try:
                log: list = json.loads(existing) if existing else []
            except (json.JSONDecodeError, TypeError):
                # Reset rather than abort so the dispute can still accept new
                # audit entries. The prior log bytes are lost, but the
                # alternative — refusing all future audit writes — is worse.
                # WARN so the corruption is visible without flooding logs.
                _LOG.warning(
                    "dispute.audit_log_corrupted dispute_id=%s prefix=%r — resetting to []",
                    dispute_id,
                    existing[:128] if isinstance(existing, str) else existing,
                )
                log = []
            log.append(entry)
            conn.execute(
                "UPDATE disputes SET audit_log = %s WHERE dispute_id = %s",
                (json.dumps(log), dispute_id),
            )
    except Exception:  # noqa: BLE001 — audit must not break callers
        _LOG.warning("Failed to append audit log entry for dispute %s", dispute_id, exc_info=True)


def get_judgments(dispute_id: str) -> list[dict]:
    """Fetch all judge votes for a dispute, ordered oldest-first."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM dispute_judgments
            WHERE dispute_id = %s
            ORDER BY created_at ASC, judgment_id ASC
            """,
            (dispute_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_dispute_context(dispute_id: str) -> dict | None:
    """Fetch the full context needed to judge a dispute: job details, messages, ratings, and prior judgments.

    Returns None if the dispute or its linked job does not exist.
    """
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT
                d.*,
                j.job_id AS ctx_job_id,
                j.agent_id,
                j.agent_owner_id,
                j.caller_owner_id,
                j.input_payload,
                j.output_payload,
                j.error_message,
                j.status AS job_status,
                j.price_cents,
                j.charge_tx_id,
                j.completed_at,
                j.settled_at,
                a.input_schema
            FROM disputes d
            JOIN jobs j ON j.job_id = d.job_id
            LEFT JOIN agents a ON a.agent_id = j.agent_id
            WHERE d.dispute_id = %s
            """,
            (dispute_id,),
        ).fetchone()
    if row is None:
        return None

    data = dict(row)
    dispute = {
        key: data[key]
        for key in (
            "dispute_id",
            "job_id",
            "filed_by_owner_id",
            "side",
            "reason",
            "evidence",
            "filing_deposit_cents",
            "status",
            "outcome",
            "split_caller_cents",
            "split_agent_cents",
            "filed_at",
            "resolved_at",
        )
    }
    for field in ("split_caller_cents", "split_agent_cents"):
        value = dispute.get(field)
        dispute[field] = int(value) if value is not None else None

    def _parse_json(value: str | None, default):
        if value is None:
            return default
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return default
        return parsed

    return {
        "dispute": dispute,
        "job": {
            "job_id": data["ctx_job_id"],
            "agent_id": data["agent_id"],
            "agent_owner_id": data["agent_owner_id"],
            "caller_owner_id": data["caller_owner_id"],
            "input_payload": _parse_json(data.get("input_payload"), {}),
            "output_payload": _parse_json(data.get("output_payload"), None),
            "error_message": data.get("error_message"),
            "status": data.get("job_status"),
            "price_cents": int(data.get("price_cents") or 0),
            "charge_tx_id": data.get("charge_tx_id"),
            "completed_at": data.get("completed_at"),
            "settled_at": data.get("settled_at"),
        },
        "agent_input_schema": _parse_json(data.get("input_schema"), {}),
        "judgments": get_judgments(dispute_id),
    }
