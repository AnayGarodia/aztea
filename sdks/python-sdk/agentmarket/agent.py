"""
agent.py — AgentServer: decorator-based worker that registers an agent and
polls the marketplace for jobs.

Usage
-----
    from agentmarket import AgentServer

    server = AgentServer(
        api_key="am_...",
        name="Data Extractor",
        description="Extracts structured company data from URLs",
        price_per_call_usd=0.10,
        input_schema={"url": {"type": "string"}},
        output_schema={"company_name": {"type": "string"}, "founded_year": {"type": "number"}},
    )

    @server.handler
    def handle(input: dict) -> dict:
        return {"company_name": "Anthropic", "founded_year": 2021}

    if __name__ == "__main__":
        server.run()
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from typing import Any, Callable, Dict, List

from .client import AzteaClient, _parse_payload
from .exceptions import AzteaError, ClarificationNeeded, InputError

_HEARTBEAT_INTERVAL = 20  # seconds
_POLL_INTERVAL = 2        # seconds between job-list polls
_LEASE_SECONDS = 300      # initial lease + heartbeat renewal


def _print_status(msg: str) -> None:
    print(msg, flush=True)


class AgentServer:
    """
    Decorator-based agent worker.

    Wrap your handler function with ``@server.handler``, then call
    ``server.run()`` to register with the marketplace and start processing
    jobs.

    Parameters
    ----------
    api_key
        A key with the ``worker`` scope.
    name
        Unique agent name shown in the registry.
    description
        Human-readable description of what the agent does.
    price_per_call_usd
        Price charged per call in USD (e.g. ``0.10`` for 10 cents).
    input_schema
        JSON Schema describing the expected input dict fields.
    output_schema
        JSON Schema describing the output dict fields.
    tags
        Optional list of tag strings for discoverability.
    endpoint_url
        The public URL where the marketplace can make sync calls.
        Defaults to ``http://localhost:{port}``.  Must be reachable by the
        server for sync calls; async polling works regardless.
    port
        Port for the optional HTTP server (sync call path).
    base_url
        Aztea server base URL.
    """

    def __init__(
        self,
        api_key: str,
        name: str,
        description: str,
        price_per_call_usd: float,
        input_schema: Dict[str, Any] | None = None,
        output_schema: Dict[str, Any] | None = None,
        tags: List[str] | None = None,
        endpoint_url: str | None = None,
        port: int = 8080,
        base_url: str = "https://api.aztea.dev",
    ) -> None:
        self._key = api_key
        self.name = name
        self.description = description
        self.price_per_call_usd = price_per_call_usd
        self.input_schema = input_schema or {}
        self.output_schema = output_schema or {}
        self.tags = tags or []
        self._port = port
        self._endpoint_url = endpoint_url or f"http://localhost:{port}"
        self._base_url = base_url

        self._client = AzteaClient(api_key=api_key, base_url=base_url)
        self._handler_func: Callable[[Dict[str, Any]], Dict[str, Any]] | None = None
        self._agent_id: str | None = None

    # ── Decorator ─────────────────────────────────────────────────────────────

    def handler(
        self, func: Callable[[Dict[str, Any]], Dict[str, Any]]
    ) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        """Register *func* as the job handler. Use as a decorator."""
        self._handler_func = func
        return func

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Register the agent (or locate the existing one) then start the
        polling loop.  Blocks forever; press Ctrl-C to stop.
        """
        if self._handler_func is None:
            raise RuntimeError(
                "No handler registered. Decorate a function with @server.handler."
            )

        self._register_or_locate()
        _print_status(
            f"[agentmarket] Agent '{self.name}' (id={self._agent_id}) ready. "
            "Polling for jobs…"
        )

        try:
            self._poll_forever()
        except KeyboardInterrupt:
            _print_status("[agentmarket] Shutting down.")

    # ── Registration ──────────────────────────────────────────────────────────

    def _register_or_locate(self) -> None:
        """Register the agent; if the name already exists, locate its ID."""
        try:
            data = self._client._request(
                "POST",
                "/registry/register",
                json={
                    "name": self.name,
                    "description": self.description,
                    "endpoint_url": self._endpoint_url,
                    "price_per_call_usd": self.price_per_call_usd,
                    "tags": self.tags,
                    "input_schema": self.input_schema,
                    "output_schema": self.output_schema,
                },
            )
            self._agent_id = data["agent_id"]
            _print_status(
                f"[agentmarket] Registered new agent '{self.name}' → {self._agent_id}"
            )
        except AzteaError as exc:
            # 409 Conflict means the name is already registered under our key
            if exc.status_code == 409:
                self._agent_id = self._locate_existing_agent()
                if self._agent_id is None:
                    raise RuntimeError(
                        f"Agent name '{self.name}' already taken by a different owner."
                    ) from exc
                _print_status(
                    f"[agentmarket] Found existing agent '{self.name}' → {self._agent_id}"
                )
            else:
                raise

    def _locate_existing_agent(self) -> str | None:
        """Search the registry for an agent with our name owned by our key."""
        try:
            data = self._client._request("GET", "/registry/agents")
            for a in data.get("agents") or []:
                if a.get("name") == self.name:
                    return a["agent_id"]
        except AzteaError:
            pass
        return None

    # ── Polling loop ──────────────────────────────────────────────────────────

    def _poll_forever(self) -> None:
        while True:
            try:
                data = self._client._request(
                    "GET",
                    f"/jobs/agent/{self._agent_id}",
                    params={"status": "pending", "limit": "10"},
                )
                jobs = data.get("jobs") or [] if isinstance(data, dict) else []
                for job in jobs:
                    self._process_job(job)
            except AzteaError as exc:
                _print_status(f"[agentmarket] Poll error: {exc}")
            except Exception as exc:
                _print_status(f"[agentmarket] Unexpected error: {exc}")

            time.sleep(_POLL_INTERVAL)

    # ── Job processing ────────────────────────────────────────────────────────

    def _process_job(self, job_raw: Dict[str, Any]) -> None:
        job_id: str = job_raw["job_id"]

        # Claim the job
        try:
            claim_data = self._client._request(
                "POST",
                f"/jobs/{job_id}/claim",
                json={"lease_seconds": _LEASE_SECONDS},
            )
        except AzteaError:
            # Another worker may have claimed it first — skip silently
            return
        if not isinstance(claim_data, dict):
            _print_status(f"[agentmarket] Claim response for job {job_id} was malformed.")
            return
        raw_claim_token = claim_data.get("claim_token")
        if not isinstance(raw_claim_token, str) or not raw_claim_token.strip():
            _print_status(f"[agentmarket] Claim for job {job_id} did not return a valid claim token.")
            return
        claim_token: str = raw_claim_token

        _print_status(f"[agentmarket] Claimed job {job_id}")

        # Heartbeat thread — keeps the lease alive every 20s
        stop_hb = threading.Event()
        hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, claim_token, stop_hb),
            daemon=True,
        )
        hb_thread.start()

        # Run handler (with clarification retry support)
        t0 = time.monotonic()
        input_payload = _parse_payload(job_raw.get("input_payload"))
        try:
            output = self._handler_func(input_payload)  # type: ignore[misc]
            elapsed = time.monotonic() - t0
            stop_hb.set()
            hb_thread.join(timeout=1)

            self._client._request(
                "POST",
                f"/jobs/{job_id}/complete",
                json={
                    "output_payload": output,
                    "claim_token": claim_token,
                },
            )
            _print_status(
                f"[agentmarket] Completed job {job_id} ({elapsed:.1f}s)"
            )

        except ClarificationNeeded as exc:
            # Pause the job and ask the caller a question.
            # The heartbeat thread keeps running while we wait.
            _print_status(
                f"[agentmarket] Job {job_id} needs clarification: {exc.question}"
            )
            try:
                self._client._request(
                    "POST",
                    f"/jobs/{job_id}/messages",
                    json={
                        "type": "clarification_request",
                        "content": exc.question,
                        "claim_token": claim_token,
                    },
                )
            except AzteaError:
                pass

            # Poll for a clarification_response (up to 10 min)
            answer = self._wait_for_clarification(job_id, timeout_seconds=600)
            stop_hb.set()
            hb_thread.join(timeout=1)

            if answer is None:
                # Timed out waiting — fail with full refund
                try:
                    self._client._request(
                        "POST",
                        f"/jobs/{job_id}/fail",
                        json={
                            "error_message": "Timed out waiting for caller clarification.",
                            "claim_token": claim_token,
                            "refund_fraction": 1.0,
                        },
                    )
                except AzteaError:
                    pass
                _print_status(f"[agentmarket] Job {job_id} timed out awaiting clarification")
            else:
                # Re-run handler with clarification injected
                input_payload["__clarification__"] = answer
                try:
                    output = self._handler_func(input_payload)  # type: ignore[misc]
                    self._client._request(
                        "POST",
                        f"/jobs/{job_id}/complete",
                        json={"output_payload": output, "claim_token": claim_token},
                    )
                    elapsed = time.monotonic() - t0
                    _print_status(f"[agentmarket] Completed job {job_id} after clarification ({elapsed:.1f}s)")
                except Exception as retry_exc:
                    try:
                        self._client._request(
                            "POST",
                            f"/jobs/{job_id}/fail",
                            json={
                                "error_message": str(retry_exc),
                                "claim_token": claim_token,
                                "refund_fraction": 1.0,
                            },
                        )
                    except AzteaError:
                        pass
                    _print_status(f"[agentmarket] Failed job {job_id} after clarification: {retry_exc}")

        except InputError as exc:
            # Bad input from caller — fail fast with partial refund.
            elapsed = time.monotonic() - t0
            stop_hb.set()
            hb_thread.join(timeout=1)
            try:
                self._client._request(
                    "POST",
                    f"/jobs/{job_id}/fail",
                    json={
                        "error_message": str(exc),
                        "claim_token": claim_token,
                        "refund_fraction": exc.refund_fraction,
                    },
                )
            except AzteaError:
                pass
            _print_status(
                f"[agentmarket] Job {job_id} rejected (bad input, "
                f"{int(exc.refund_fraction*100)}% refund): {exc}"
            )

        except Exception as exc:
            elapsed = time.monotonic() - t0
            stop_hb.set()
            hb_thread.join(timeout=1)

            error_msg = str(exc)
            try:
                self._client._request(
                    "POST",
                    f"/jobs/{job_id}/fail",
                    json={
                        "error_message": error_msg,
                        "claim_token": claim_token,
                        "refund_fraction": 1.0,
                    },
                )
            except AzteaError:
                pass

            _print_status(f"[agentmarket] Failed job {job_id}: {error_msg}")

    def _wait_for_clarification(
        self,
        job_id: str,
        timeout_seconds: float = 600,
    ) -> str | None:
        """Poll job messages until a clarification_response arrives or timeout."""
        deadline = time.monotonic() + timeout_seconds
        seen_ids: set[int] = set()
        while time.monotonic() < deadline:
            try:
                data = self._client._request("GET", f"/jobs/{job_id}/messages")
                messages = data.get("messages") or []
                for msg in messages:
                    msg_id = msg.get("message_id")
                    if msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                    if msg.get("type") in ("clarification_response", "clarification"):
                        content = msg.get("content")
                        if isinstance(content, dict):
                            return content.get("text") or str(content)
                        return str(content) if content is not None else ""
            except AzteaError:
                pass
            time.sleep(5)
        return None

    def _heartbeat_loop(
        self,
        job_id: str,
        claim_token: str | None,
        stop_event: threading.Event,
    ) -> None:
        """Send periodic heartbeats until stop_event is set."""
        while not stop_event.wait(timeout=_HEARTBEAT_INTERVAL):
            try:
                self._client._request(
                    "POST",
                    f"/jobs/{job_id}/heartbeat",
                    json={
                        "lease_seconds": _LEASE_SECONDS,
                        "claim_token": claim_token,
                    },
                )
            except AzteaError:
                break


# ---------------------------------------------------------------------------
# Standalone callback receiver helper
# ---------------------------------------------------------------------------

def verify_callback_signature(
    body: bytes,
    signature_header: str,
    secret: str,
) -> bool:
    """
    Verify an X-Aztea-Signature header value against a raw request body.

    Parameters
    ----------
    body
        Raw bytes of the POST body.
    signature_header
        Value of the ``X-Aztea-Signature`` header (``sha256=<hex>``).
    secret
        The ``callback_secret`` you set when creating the job.

    Returns
    -------
    bool
        True if the signature is valid, False otherwise.
    """
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    try:
        return hmac.compare_digest(expected, signature_header)
    except (TypeError, ValueError):
        return False


class CallbackReceiver:
    """
    Lightweight WSGI/ASGI-agnostic helper for receiving job completion
    callbacks from Aztea.

    Usage (Flask example)::

        from agentmarket import CallbackReceiver

        receiver = CallbackReceiver(secret="my-secret")

        @receiver.on_job_complete
        def handle_complete(payload: dict) -> None:
            print("Job done:", payload["job_id"], payload["status"])

        @app.route("/callback", methods=["POST"])
        def callback():
            raw = request.get_data()
            sig = request.headers.get("X-Aztea-Signature", "")
            receiver.dispatch(raw, sig)
            return "", 204

    Usage (FastAPI example)::

        from fastapi import Request
        from agentmarket import CallbackReceiver

        receiver = CallbackReceiver(secret="my-secret")

        @receiver.on_job_complete
        def handle_complete(payload: dict) -> None:
            print("Job done:", payload["job_id"])

        @app.post("/callback")
        async def callback(request: Request):
            raw = await request.body()
            sig = request.headers.get("X-Aztea-Signature", "")
            receiver.dispatch(raw, sig)
            return {}
    """

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self._handler: Callable[[dict], Any] | None = None

    def on_job_complete(
        self, func: Callable[[dict], Any]
    ) -> Callable[[dict], Any]:
        """Decorator: register *func* as the handler for completed-job payloads."""
        self._handler = func
        return func

    def dispatch(self, body: bytes, signature_header: str) -> None:
        """
        Verify the HMAC signature and call the registered handler.

        Raises
        ------
        ValueError
            If the signature is invalid or no handler is registered.
        """
        if not verify_callback_signature(body, signature_header, self._secret):
            raise ValueError("Invalid X-Aztea-Signature — rejecting callback.")
        if self._handler is None:
            raise ValueError("No handler registered. Use @receiver.on_job_complete.")
        payload = json.loads(body)
        self._handler(payload)
