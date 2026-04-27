from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from typing import Any, Callable

from .client import AzteaClient, _coerce_payload
from .errors import AzteaError, ClarificationNeeded, InputError

_HEARTBEAT_INTERVAL = 20
_POLL_INTERVAL = 2
_LEASE_SECONDS = 300


def verify_callback_signature(body: bytes, signature_header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, signature_header)
    except (TypeError, ValueError):
        return False


class CallbackReceiver:
    def __init__(self, secret: str) -> None:
        self._secret = secret
        self._handler: Callable[[dict[str, Any]], Any] | None = None

    def on_job_complete(self, func: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        self._handler = func
        return func

    def dispatch(self, body: bytes, signature_header: str) -> None:
        if not verify_callback_signature(body, signature_header, self._secret):
            raise ValueError("Invalid X-Aztea-Signature; rejecting callback.")
        if self._handler is None:
            raise ValueError("No handler registered. Use @receiver.on_job_complete.")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("Callback payload must be a JSON object.")
        self._handler(payload)


class AgentServer:
    def __init__(
        self,
        api_key: str,
        name: str,
        description: str,
        price_per_call_usd: float,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        endpoint_url: str | None = None,
        port: int = 8080,
        base_url: str = "http://localhost:8000",
    ) -> None:
        self._client = AzteaClient(base_url=base_url, api_key=api_key, client_id="aztea-python-agent-server")
        self.name = name
        self.description = description
        self.price_per_call_usd = price_per_call_usd
        self.input_schema = input_schema or {}
        self.output_schema = output_schema or {}
        self.tags = tags or []
        self._endpoint_url = endpoint_url or f"http://localhost:{port}"
        self._handler_func: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self._agent_id: str | None = None

    def handler(self, func: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        self._handler_func = func
        return func

    def run(self) -> None:
        if self._handler_func is None:
            raise RuntimeError("No handler registered. Decorate a function with @server.handler.")
        self._register_or_locate()
        try:
            self._poll_forever()
        except KeyboardInterrupt:
            return

    def _register_or_locate(self) -> None:
        try:
            data = self._client.registry.register(
                name=self.name,
                description=self.description,
                endpoint_url=self._endpoint_url,
                price_per_call_usd=self.price_per_call_usd,
                tags=self.tags,
                input_schema=self.input_schema,
                output_schema=self.output_schema,
            )
            self._agent_id = str(data["agent_id"])
        except AzteaError as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code != 409:
                raise
            self._agent_id = self._locate_existing_agent()
            if self._agent_id is None:
                raise RuntimeError(f"Agent name '{self.name}' already taken by a different owner.") from exc

    def _locate_existing_agent(self) -> str | None:
        data = self._client.registry.list()
        for item in data.get("agents") or []:
            if isinstance(item, dict) and item.get("name") == self.name:
                raw = item.get("agent_id")
                if isinstance(raw, str):
                    return raw
        return None

    def _poll_forever(self) -> None:
        while True:
            try:
                data = self._client.jobs.list_for_agent(str(self._agent_id), status="pending", limit=10)
                for job in data.get("jobs") or []:
                    if isinstance(job, dict):
                        self._process_job(job)
            except AzteaError:
                pass
            time.sleep(_POLL_INTERVAL)

    def _process_job(self, job_raw: dict[str, Any]) -> None:
        job_id = str(job_raw["job_id"])
        try:
            claim_data = self._client.jobs.claim(job_id, lease_seconds=_LEASE_SECONDS)
        except AzteaError:
            return
        claim_token = claim_data.get("claim_token")
        if not isinstance(claim_token, str) or not claim_token.strip():
            return

        stop_hb = threading.Event()
        hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, claim_token, stop_hb),
            daemon=True,
        )
        hb_thread.start()

        input_payload = _coerce_payload(job_raw.get("input_payload"))
        try:
            output = self._run_handler(input_payload)
            stop_hb.set()
            hb_thread.join(timeout=1)
            self._client.jobs.complete(job_id, output, claim_token=claim_token)
        except ClarificationNeeded as exc:
            self._client.jobs.post_message(job_id, "clarification_request", {"question": exc.question})
            answer = self._wait_for_clarification(job_id, timeout_seconds=600)
            stop_hb.set()
            hb_thread.join(timeout=1)
            if answer is None:
                self._client.jobs.fail(job_id, "Timed out waiting for caller clarification.", claim_token=claim_token)
                return
            input_payload["__clarification__"] = answer
            try:
                output = self._run_handler(input_payload)
                self._client.jobs.complete(job_id, output, claim_token=claim_token)
            except Exception as retry_exc:
                self._client.jobs.fail(job_id, str(retry_exc), claim_token=claim_token)
        except InputError as exc:
            stop_hb.set()
            hb_thread.join(timeout=1)
            self._client.jobs.fail(job_id, str(exc), claim_token=claim_token)
        except Exception as exc:
            stop_hb.set()
            hb_thread.join(timeout=1)
            self._client.jobs.fail(job_id, str(exc), claim_token=claim_token)

    def _run_handler(self, input_payload: dict[str, Any]) -> dict[str, Any]:
        if self._handler_func is None:
            raise RuntimeError("No handler registered.")
        return self._handler_func(input_payload)

    def _wait_for_clarification(self, job_id: str, timeout_seconds: int) -> str | None:
        deadline = time.monotonic() + timeout_seconds
        seen_ids: set[int] = set()
        while time.monotonic() < deadline:
            messages = self._client.jobs.list_messages(job_id).get("messages") or []
            for message in reversed(messages):
                if not isinstance(message, dict):
                    continue
                msg_id = message.get("message_id")
                if isinstance(msg_id, int) and msg_id in seen_ids:
                    continue
                if isinstance(msg_id, int):
                    seen_ids.add(msg_id)
                if message.get("type") != "clarification_response":
                    continue
                payload = message.get("payload")
                if isinstance(payload, dict):
                    answer = payload.get("answer")
                    if isinstance(answer, str):
                        return answer
            time.sleep(_POLL_INTERVAL)
        return None

    def _heartbeat_loop(self, job_id: str, claim_token: str, stop_event: threading.Event) -> None:
        while not stop_event.wait(timeout=_HEARTBEAT_INTERVAL):
            try:
                self._client.jobs.heartbeat(job_id, claim_token, lease_seconds=_LEASE_SECONDS)
            except AzteaError:
                break
