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

import json
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .client import AgentMarketClient, _parse_payload
from .exceptions import AgentMarketError
from .models import Agent

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
        AgentMarket server base URL.
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
        base_url: str = "https://api.agentmarket.dev",
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

        self._client = AgentMarketClient(api_key=api_key, base_url=base_url)
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
        except AgentMarketError as exc:
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
        except AgentMarketError:
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
            except AgentMarketError as exc:
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
            claim_token: str | None = claim_data.get("claim_token")
        except AgentMarketError as exc:
            # Another worker may have claimed it first — skip silently
            return

        _print_status(f"[agentmarket] Claimed job {job_id}")

        # Heartbeat thread — keeps the lease alive every 20s
        stop_hb = threading.Event()
        hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, claim_token, stop_hb),
            daemon=True,
        )
        hb_thread.start()

        # Run handler
        t0 = time.monotonic()
        try:
            input_payload = _parse_payload(job_raw.get("input_payload"))
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
                    },
                )
            except AgentMarketError:
                pass

            _print_status(f"[agentmarket] Failed job {job_id}: {error_msg}")

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
            except AgentMarketError:
                break
