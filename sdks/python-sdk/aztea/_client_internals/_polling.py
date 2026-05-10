from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ..errors import ClarificationNeededError, JobFailedError, AzteaError
from ..models import JobResult, VerificationContract
from ._helpers import _coerce_payload, _verify_contract

if TYPE_CHECKING:
    from .client_core import AzteaClient


def poll_job_to_completion(
    client: "AzteaClient",
    job_id: str,
    *,
    timeout_seconds: int,
    verification_contract: VerificationContract | dict[str, Any] | None = None,
) -> JobResult:
    deadline = time.monotonic() + timeout_seconds
    contract = (
        VerificationContract(**verification_contract)
        if isinstance(verification_contract, dict)
        else verification_contract
    )
    while True:
        if time.monotonic() > deadline:
            raise AzteaError(f"Job {job_id} did not complete within {timeout_seconds}s.")
        job = client.jobs.get_raw(job_id)
        status = str(job.get("status") or "")
        if status in ("complete", "stopped"):
            output = _coerce_payload(job.get("output_payload"))
            # Skip contract verification for stop_when-aborted jobs since the
            # output is a partial — verifying a partial against a complete-job
            # contract would always fail and obscure the real reason for stop.
            if contract is not None and status == "complete":
                _verify_contract(output, contract)
            return JobResult(
                job_id=job_id,
                output=output,
                quality_score=job.get("quality_score"),
                cost_cents=int(job.get("price_cents") or 0),
            ).bind_client(client)
        if status == "failed":
            raise JobFailedError(
                str(job.get("error_message") or "Job failed."),
                _coerce_payload(job.get("output_payload")),
            )
        if status == "awaiting_clarification":
            messages = client.jobs.list_messages(job_id).get("messages") or []
            question = "Agent needs clarification."
            for item in reversed(messages):
                if isinstance(item, dict) and item.get("type") == "clarification_request":
                    payload = item.get("payload")
                    if isinstance(payload, dict) and isinstance(payload.get("question"), str):
                        question = payload["question"]
                        break
            raise ClarificationNeededError(question, job_id)
        time.sleep(2.0)
