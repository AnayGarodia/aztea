"""Shared test helpers for the `aztea dispute` CLI suite.

Every test file in `test_cli_dispute*.py` imports from here so the mocks
stay consistent. Keep this module dependency-light — only `dataclasses`,
`typing`, and the SDK's own surface.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Default policy + dispute payloads
# ---------------------------------------------------------------------------


DEFAULT_POLICY: dict[str, Any] = {
    "filing_deposit_bps": 500,
    "filing_deposit_min_cents": 5,
    "default_dispute_window_hours": 72,
    "judges_required": 2,
    "judges_total": 3,
    "formula": (
        "deposit_cents = max(filing_deposit_min_cents, "
        "price_cents * filing_deposit_bps / 10000)"
    ),
}


DEFAULT_DISPUTE_RECEIPT: dict[str, Any] = {
    "dispute_id": "dsp_1",
    "status": "pending",
    "side": "caller",
    "filing_deposit_cents": 5,
}


DEFAULT_DISPUTE_STATUS: dict[str, Any] = {
    "dispute_id": "dsp_1",
    "job_id": "job-1",
    "status": "pending",
    "side": "caller",
    "filed_at": "2026-05-09T00:00:00+00:00",
    "filing_deposit_cents": 5,
    "judgments": [],
}


# ---------------------------------------------------------------------------
# Job factory
# ---------------------------------------------------------------------------


def make_job(
    *,
    job_id: str = "job-1",
    agent_id: str = "agent-1",
    agent_name: str = "wiki-summary",
    caller_owner_id: str = "owner-self",
    status: str = "complete",
    price_cents: int = 100,
    completed_at: datetime | str | None | object = ...,
    created_at: datetime | str | None | object = ...,
    disputable: bool = True,
    disputable_reason: str | None = None,
    disputable_code: str | None = None,
    input_payload: dict | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a job dict in the shape `_job_response` produces.

    `completed_at` / `created_at` default to "5 minutes ago" if the sentinel
    is left untouched. Pass ``None`` explicitly to drop the field.
    """
    five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    if completed_at is ...:
        completed_at = five_min_ago.isoformat()
    elif isinstance(completed_at, datetime):
        completed_at = completed_at.isoformat()
    if created_at is ...:
        created_at = one_hour_ago.isoformat()
    elif isinstance(created_at, datetime):
        created_at = created_at.isoformat()

    job: dict[str, Any] = {
        "job_id": job_id,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "caller_owner_id": caller_owner_id,
        "status": status,
        "price_cents": price_cents,
        "caller_charge_cents": price_cents,
        "dispute_window_hours": 72,
        "disputable": disputable,
        "disputable_reason": disputable_reason,
        "disputable_code": disputable_code,
    }
    if completed_at is not None:
        job["completed_at"] = completed_at
    if created_at is not None:
        job["created_at"] = created_at
    if input_payload is not None:
        job["input_payload"] = input_payload
    job.update(extra)
    return job


def make_ineligible_job(
    *,
    code: str,
    message: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Shortcut for an ineligible job — fills `disputable_reason` from the
    `core/jobs/disputable.py` table when caller doesn't pass one."""
    if message is None:
        message = _DEFAULT_INELIGIBLE_MESSAGES.get(code, "Job is not disputable.")
    return make_job(
        disputable=False,
        disputable_code=code,
        disputable_reason=message,
        **kwargs,
    )


_DEFAULT_INELIGIBLE_MESSAGES: dict[str, str] = {
    "dispute.not_completed": (
        "Disputes can only be filed for jobs that produced output "
        "(completed_at is unset)."
    ),
    "dispute.window_expired": "Dispute window has expired for this job.",
    "dispute.already_filed": "A dispute already exists for this job.",
    "dispute.already_rated": (
        "You already rated this job; disputes can only be filed before "
        "submitting a rating."
    ),
    "dispute.invalid_window": "Dispute window could not be computed for this job.",
}


# ---------------------------------------------------------------------------
# Fake SDK client
# ---------------------------------------------------------------------------


class FakeDisputeClient:
    """Mock of `AzteaClient` covering the dispute CLI's needs.

    Each method returns the `_*_result` attribute by default. Tests can
    override per-instance via constructor kwargs, then assert against
    `dispute_calls` / `list_jobs_calls` / `get_dispute_calls` to verify
    the CLI made the right requests.
    """

    def __init__(
        self,
        *args: Any,
        list_jobs_response: dict | None = None,
        dispute_result: dict | None = None,
        get_dispute_result: dict | None = None,
        get_dispute_raises: Exception | None = None,
        dispute_raises: Exception | None = None,
        list_jobs_raises: Exception | None = None,
        policy: dict | None = None,
        policy_raises: Exception | None = None,
        get_job_result: dict | None = None,
        get_job_raises: Exception | None = None,
        **kwargs: Any,
    ) -> None:
        self._list_jobs_response = (
            list_jobs_response
            if list_jobs_response is not None
            else {"jobs": [], "next_cursor": None}
        )
        self._dispute_result = (
            dispute_result if dispute_result is not None else dict(DEFAULT_DISPUTE_RECEIPT)
        )
        self._get_dispute_result = (
            get_dispute_result
            if get_dispute_result is not None
            else dict(DEFAULT_DISPUTE_STATUS)
        )
        self._get_dispute_raises = get_dispute_raises
        self._dispute_raises = dispute_raises
        self._list_jobs_raises = list_jobs_raises
        self._policy = policy if policy is not None else dict(DEFAULT_POLICY)
        self._policy_raises = policy_raises
        self._get_job_result = (
            get_job_result if get_job_result is not None else {"price_cents": 100}
        )
        self._get_job_raises = get_job_raises

        # Call records for assertion.
        self.dispute_calls: list[dict[str, Any]] = []
        self.list_jobs_calls: list[dict[str, Any]] = []
        self.get_dispute_calls: list[str] = []
        self.get_job_calls: list[str] = []
        self.policy_calls: int = 0

    # Context-manager surface so `with _open_client(...) as client:` works.
    def __enter__(self) -> "FakeDisputeClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def close(self) -> None:
        return None

    # ─── SDK methods the CLI calls ──────────────────────────────────────

    def list_jobs(
        self, *, limit: int = 50, status: str | None = None, cursor: str | None = None
    ) -> dict:
        self.list_jobs_calls.append({"limit": limit, "status": status, "cursor": cursor})
        if self._list_jobs_raises is not None:
            raise self._list_jobs_raises
        return self._list_jobs_response

    def get_dispute_policy(self) -> dict:
        self.policy_calls += 1
        if self._policy_raises is not None:
            raise self._policy_raises
        return self._policy

    def get_job(self, job_id: str) -> dict:
        self.get_job_calls.append(job_id)
        if self._get_job_raises is not None:
            raise self._get_job_raises
        return self._get_job_result

    def dispute_job(
        self,
        job_id: str,
        *,
        reason: str,
        evidence: str | None = None,
    ) -> dict:
        self.dispute_calls.append(
            {"job_id": job_id, "reason": reason, "evidence": evidence}
        )
        if self._dispute_raises is not None:
            raise self._dispute_raises
        return self._dispute_result

    def get_dispute(self, job_id: str) -> dict:
        self.get_dispute_calls.append(job_id)
        if self._get_dispute_raises is not None:
            raise self._get_dispute_raises
        return self._get_dispute_result

    # `_request_json("GET", "/auth/me")` is hit by the wizard to resolve
    # owner_id for caller/worker labelling.
    def _request_json(self, method: str, path: str, **_: Any) -> dict:
        if path == "/auth/me":
            return {"owner_id": "owner-self"}
        return {}


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------


def patch_client(monkeypatch, fake: FakeDisputeClient) -> None:
    """Make `aztea.cli._client(...)` return `fake`."""
    monkeypatch.setattr("aztea.cli._client", lambda **_: fake)


def patch_tty(monkeypatch, value: bool = True) -> None:
    """Force the wizard's TTY check to return `value`."""
    monkeypatch.setattr("aztea.cli.dispute_wizard._is_tty", lambda: value)
    monkeypatch.setattr("aztea.cli.dispute._is_tty", lambda: value)
