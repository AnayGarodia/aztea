from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol

from .errors import APIError, ClaimLostError
from .types import JSONObject

if TYPE_CHECKING:
    from .client import AzteaClient


class JobSource(Protocol):
    def fetch_pending_jobs(self, client: "AzteaClient", agent_id: str, limit: int) -> list[JSONObject]:
        ...


class PollingJobSource:
    def __init__(self, poll_status: str = "pending") -> None:
        self._poll_status = poll_status

    def fetch_pending_jobs(self, client: "AzteaClient", agent_id: str, limit: int) -> list[JSONObject]:
        payload = client.jobs.list_for_agent(agent_id, status=self._poll_status, limit=limit)
        jobs = payload.get("jobs")
        if isinstance(jobs, list):
            return [item for item in jobs if isinstance(item, dict)]
        return []


WorkerFunction = Callable[[JSONObject], JSONObject]


@dataclass
class WorkerRunner:
    client: "AzteaClient"
    agent_id: str
    handler: WorkerFunction
    concurrency: int
    lease_seconds: int
    poll_interval: float
    job_source: JobSource

    def __post_init__(self) -> None:
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run_once(self) -> int:
        pending = self.job_source.fetch_pending_jobs(self.client, self.agent_id, self.concurrency)
        if not pending:
            return 0
        with ThreadPoolExecutor(max_workers=max(1, self.concurrency)) as pool:
            futures = [pool.submit(self._process_single_job, job_item) for job_item in pending[: self.concurrency]]
            for future in futures:
                future.result()
        return len(pending[: self.concurrency])

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            processed = self.run_once()
            if processed == 0:
                self._stop_event.wait(timeout=max(0.1, self.poll_interval))

    def _process_single_job(self, job_item: JSONObject) -> None:
        raw_job_id = job_item.get("job_id")
        if not isinstance(raw_job_id, str) or not raw_job_id:
            return
        job_id = raw_job_id

        claimed = self.client.jobs.claim(job_id, lease_seconds=self.lease_seconds)
        claim_token_raw = claimed.get("claim_token")
        if not isinstance(claim_token_raw, str) or not claim_token_raw:
            return
        claim_token = claim_token_raw

        lease_lost_event = threading.Event()
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, claim_token, heartbeat_stop, lease_lost_event),
            name=f"aztea-heartbeat-{job_id}",
            daemon=True,
        )
        heartbeat_thread.start()

        try:
            input_payload = job_item.get("input_payload")
            payload = input_payload if isinstance(input_payload, dict) else {}
            output_payload = self.handler(payload)
            if not isinstance(output_payload, dict):
                raise ValueError("Worker handler must return a dict payload.")
            if lease_lost_event.is_set():
                raise ClaimLostError(410, "Claim lease was lost before completion.", "lease lost", {})
            self.client.jobs.complete(job_id, output_payload=output_payload, claim_token=claim_token)
        except ClaimLostError:
            return
        except Exception as exc:
            try:
                self.client.jobs.fail(job_id, error_message=str(exc), claim_token=claim_token)
            except APIError:
                return
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(1.0, self.poll_interval))
            try:
                self.client.jobs.release(job_id, claim_token=claim_token)
            except APIError:
                pass

    def _heartbeat_loop(
        self,
        job_id: str,
        claim_token: str,
        stop_event: threading.Event,
        lease_lost_event: threading.Event,
    ) -> None:
        interval = max(1.0, self.lease_seconds / 3.0)
        while not stop_event.wait(timeout=interval):
            try:
                self.client.jobs.heartbeat(job_id, claim_token=claim_token, lease_seconds=self.lease_seconds)
            except ClaimLostError:
                lease_lost_event.set()
                return
            except APIError:
                continue


def build_worker_decorator(
    client: "AzteaClient",
    agent_id: str,
    *,
    concurrency: int = 1,
    lease_seconds: int = 300,
    poll_interval: float = 2.0,
    job_source: JobSource | None = None,
) -> Callable[[WorkerFunction], WorkerRunner]:
    source = job_source or PollingJobSource()

    def _decorator(handler: WorkerFunction) -> WorkerRunner:
        return WorkerRunner(
            client=client,
            agent_id=agent_id,
            handler=handler,
            concurrency=max(1, concurrency),
            lease_seconds=max(1, lease_seconds),
            poll_interval=max(0.1, poll_interval),
            job_source=source,
        )

    return _decorator
