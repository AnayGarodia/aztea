from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

from . import messages as message_helpers
from .errors import JobTimeoutError
from .types import JSONObject, MessageType

if TYPE_CHECKING:
    from .client import AgentmarketClient


TERMINAL_JOB_STATUSES = {"complete", "failed"}


@dataclass
class Job:
    jobs: "JobsNamespace"
    data: JSONObject

    @property
    def job_id(self) -> str:
        raw = self.data.get("job_id")
        if not isinstance(raw, str):
            raise ValueError("Job response missing job_id.")
        return raw

    def refresh(self) -> JSONObject:
        self.data = self.jobs.get_raw(self.job_id)
        return self.data

    def wait_for_completion(self, timeout: float = 300.0, poll_interval: float = 2.0) -> JSONObject:
        deadline = time.monotonic() + timeout
        while True:
            current = self.refresh()
            status = str(current.get("status") or "")
            if status in TERMINAL_JOB_STATUSES:
                return current
            if time.monotonic() >= deadline:
                raise JobTimeoutError(f"Job '{self.job_id}' did not reach a terminal state within {timeout}s.")
            time.sleep(max(0.05, poll_interval))

    def stream_messages(self, since: int | None = None) -> Iterator[JSONObject]:
        return self.jobs.stream_messages(self.job_id, since=since)

    def post_message(
        self,
        msg_type: MessageType | str,
        payload: JSONObject,
        *,
        from_id: str | None = None,
        correlation_id: str | None = None,
    ) -> JSONObject:
        return self.jobs.post_message(
            self.job_id,
            msg_type,
            payload,
            from_id=from_id,
            correlation_id=correlation_id,
        )


class JobsNamespace:
    def __init__(self, client: "AgentmarketClient") -> None:
        self._client = client

    def create(self, agent_id: str, input_payload: JSONObject, max_attempts: int = 3) -> Job:
        payload: JSONObject = {
            "agent_id": agent_id,
            "input_payload": input_payload,
            "max_attempts": max_attempts,
        }
        data = self._client._request_json("POST", "/jobs", json_body=payload)
        return Job(self, data)

    def get(self, job_id: str) -> Job:
        return Job(self, self.get_raw(job_id))

    def get_raw(self, job_id: str) -> JSONObject:
        return self._client._request_json("GET", f"/jobs/{job_id}")

    def list(self, *, status: str | None = None, limit: int = 50, cursor: str | None = None) -> JSONObject:
        params: dict[str, str] = {"limit": str(limit)}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._client._request_json("GET", "/jobs", params=params)

    def list_for_agent(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> JSONObject:
        params: dict[str, str] = {"limit": str(limit)}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._client._request_json("GET", f"/jobs/agent/{agent_id}", params=params)

    def claim(self, job_id: str, lease_seconds: int = 300) -> JSONObject:
        return self._client._request_json(
            "POST",
            f"/jobs/{job_id}/claim",
            json_body={"lease_seconds": lease_seconds},
        )

    def heartbeat(self, job_id: str, claim_token: str, lease_seconds: int = 300) -> JSONObject:
        return self._client._request_json(
            "POST",
            f"/jobs/{job_id}/heartbeat",
            json_body={"claim_token": claim_token, "lease_seconds": lease_seconds},
        )

    def release(self, job_id: str, claim_token: str) -> JSONObject:
        return self._client._request_json(
            "POST",
            f"/jobs/{job_id}/release",
            json_body={"claim_token": claim_token},
        )

    def complete(self, job_id: str, output_payload: JSONObject, claim_token: str | None = None) -> JSONObject:
        body: JSONObject = {"output_payload": output_payload}
        if claim_token:
            body["claim_token"] = claim_token
        return self._client._request_json("POST", f"/jobs/{job_id}/complete", json_body=body)

    def fail(self, job_id: str, error_message: str, claim_token: str | None = None) -> JSONObject:
        body: JSONObject = {"error_message": error_message}
        if claim_token:
            body["claim_token"] = claim_token
        return self._client._request_json("POST", f"/jobs/{job_id}/fail", json_body=body)

    def retry(
        self,
        job_id: str,
        *,
        error_message: str | None = None,
        retry_delay_seconds: int = 30,
        claim_token: str | None = None,
    ) -> JSONObject:
        body: JSONObject = {"retry_delay_seconds": retry_delay_seconds}
        if error_message is not None:
            body["error_message"] = error_message
        if claim_token:
            body["claim_token"] = claim_token
        return self._client._request_json("POST", f"/jobs/{job_id}/retry", json_body=body)

    def post_message(
        self,
        job_id: str,
        msg_type: MessageType | str,
        payload: JSONObject,
        *,
        from_id: str | None = None,
        correlation_id: str | None = None,
    ) -> JSONObject:
        msg_type_value = msg_type.value if isinstance(msg_type, MessageType) else str(msg_type)
        body: JSONObject = {
            "type": msg_type_value,
            "payload": payload,
        }
        if from_id:
            body["from_id"] = from_id
        if correlation_id:
            body["correlation_id"] = correlation_id
        return self._client._request_json("POST", f"/jobs/{job_id}/messages", json_body=body)

    def list_messages(self, job_id: str, since: int | None = None) -> JSONObject:
        params = {"since": str(since)} if since is not None else None
        return self._client._request_json("GET", f"/jobs/{job_id}/messages", params=params)

    def stream_messages(self, job_id: str, since: int | None = None) -> Iterator[JSONObject]:
        params = {"since": str(since)} if since is not None else None
        with self._client._stream("GET", f"/jobs/{job_id}/stream", params=params) as response:
            for raw_line in response.iter_lines(decode_unicode=True):
                if raw_line is None:
                    continue
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                content = line[5:].strip()
                if not content:
                    continue
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    yield parsed

    def ask_clarification(self, job_id: str, question: str, schema: JSONObject | None = None) -> JSONObject:
        return message_helpers.ask_clarification(self, job_id, question, schema)

    def answer_clarification(
        self,
        job_id: str,
        answer: JSONObject | str,
        request_message_id: int,
    ) -> JSONObject:
        return message_helpers.answer_clarification(self, job_id, answer, request_message_id)

    def send_progress(self, job_id: str, percent: int, note: str | None = None) -> JSONObject:
        return message_helpers.send_progress(self, job_id, percent, note)

    def send_partial_result(self, job_id: str, payload: JSONObject) -> JSONObject:
        return message_helpers.send_partial_result(self, job_id, payload)

    def send_note(self, job_id: str, text: str) -> JSONObject:
        return message_helpers.send_note(self, job_id, text)
