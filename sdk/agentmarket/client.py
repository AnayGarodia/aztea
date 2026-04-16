"""
client.py — AgentMarketClient: high-level API for callers.

Callers use this to discover agents, hire them (async or sync), and manage
their wallet.  All methods raise typed exceptions from exceptions.py rather
than returning raw error dicts.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Union

import httpx

from .exceptions import (
    AgentMarketError,
    AgentNotFoundError,
    AuthenticationError,
    ClarificationNeededError,
    ContractVerificationError,
    InsufficientFundsError,
    JobFailedError,
    PermissionError,
    RateLimitError,
)
from .models import Agent, Job, JobResult, Transaction, VerificationContract, Wallet

_VERSION_HEADER = "1.0"
_POLL_INTERVAL = 2.0  # seconds between /jobs/{id} polls


def _parse_payload(value: Any) -> Dict[str, Any]:
    """Coerce a server payload field (may arrive as string or dict) to dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


class AgentMarketClient:
    """
    High-level client for the AgentMarket platform.

    Parameters
    ----------
    api_key
        Your AgentMarket API key (starts with ``am_``).
    base_url
        Base URL of the AgentMarket server.  Defaults to the hosted platform.
        For local development use ``http://localhost:8000``.
    timeout
        Default HTTP timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.agentmarket.dev",  # override for self-hosted
        timeout: float = 30.0,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self._base,
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-AgentMarket-Version": _VERSION_HEADER,
                "Content-Type": "application/json",
                "User-Agent": f"agentmarket-python/{__import__('agentmarket').__version__}",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> "AgentMarketClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Dict[str, Any] | None = None,
    ) -> Any:
        try:
            resp = self._http.request(method, path, json=json, params=params)
        except httpx.TransportError as exc:
            raise AgentMarketError(f"Network error: {exc}") from exc

        body: Any = None
        if resp.content:
            try:
                body = resp.json()
            except Exception:
                body = resp.text

        if resp.status_code == 401:
            detail = _extract_detail(body) or "Invalid or missing API key."
            raise AuthenticationError(detail)
        if resp.status_code == 402:
            detail = _extract_detail(body) or "Insufficient funds."
            raise InsufficientFundsError(detail)
        if resp.status_code == 403:
            detail = _extract_detail(body) or "Insufficient permissions."
            raise PermissionError(detail)
        if resp.status_code == 404:
            detail = _extract_detail(body) or "Not found."
            raise AgentNotFoundError(detail)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            raise RateLimitError(retry_after)
        if not resp.is_success:
            detail = _extract_detail(body) or f"HTTP {resp.status_code}"
            raise AgentMarketError(detail, status_code=resp.status_code)

        return body

    # ── Discovery ─────────────────────────────────────────────────────────────

    def search_agents(
        self,
        query: str,
        *,
        max_price_cents: int | None = None,
        min_trust: float | None = None,
    ) -> List[Agent]:
        """
        Search the registry for agents matching *query*.

        Optional filters are applied client-side after the server returns
        ranked results.
        """
        data = self._request(
            "POST",
            "/registry/search",
            json={"query": str(query).strip()},
        )
        # Server returns {results: [{agent: {...}, similarity, ...}, ...]}
        raw_results = data.get("results") or [] if isinstance(data, dict) else []
        agents = [Agent(**item["agent"]) for item in raw_results if isinstance(item.get("agent"), dict)]

        if max_price_cents is not None:
            agents = [a for a in agents if a.price_cents <= max_price_cents]
        if min_trust is not None:
            agents = [a for a in agents if a.trust_score >= min_trust]

        return agents

    def list_agents(
        self,
        *,
        tag: str | None = None,
        rank_by: str = "trust",
    ) -> List[Agent]:
        """Return all visible agents, optionally filtered by tag and ranked."""
        params: Dict[str, Any] = {"rank_by": rank_by}
        if tag:
            params["tag"] = tag
        data = self._request("GET", "/registry/agents", params=params)
        raw_agents = data.get("agents") or [] if isinstance(data, dict) else []
        return [Agent(**a) for a in raw_agents]

    def get_agent(self, agent_id: str) -> Agent:
        """Fetch a single agent by its ID."""
        data = self._request("GET", f"/registry/agents/{agent_id}")
        return Agent(**data)

    # ── Hiring ────────────────────────────────────────────────────────────────

    def hire(
        self,
        agent_id: str,
        input_payload: Dict[str, Any],
        *,
        verification_contract: Union[VerificationContract, Dict[str, Any], None] = None,
        wait: bool = True,
        timeout_seconds: int = 60,
        max_attempts: int = 3,
    ) -> JobResult:
        """
        Create a job and (by default) block until it completes.

        Parameters
        ----------
        agent_id
            The agent to hire.
        input_payload
            Input data for the agent.
        verification_contract
            Optional contract checked against the output.  Raises
            :exc:`ContractVerificationError` on mismatch.
        wait
            If ``True`` (default) poll until the job is done and return a
            :class:`~agentmarket.models.JobResult`.  If ``False`` return
            immediately with an empty-output JobResult containing only the
            ``job_id`` and ``cost_cents``.
        timeout_seconds
            How long to wait for completion before raising ``TimeoutError``.
        max_attempts
            Max worker retry attempts for the job.
        """
        data = self._request(
            "POST",
            "/jobs",
            json={
                "agent_id": agent_id,
                "input_payload": input_payload,
                "max_attempts": max_attempts,
            },
        )
        job_id: str = data["job_id"]

        if not wait:
            return JobResult(
                job_id=job_id,
                output={},
                cost_cents=data.get("price_cents", 0),
            )

        deadline = time.monotonic() + timeout_seconds
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Job {job_id} did not complete within {timeout_seconds}s."
                )

            job_data = self._request("GET", f"/jobs/{job_id}")
            status = job_data.get("status", "")

            if status == "complete":
                output = _parse_payload(job_data.get("output_payload"))

                if verification_contract is not None:
                    contract = (
                        VerificationContract(**verification_contract)
                        if isinstance(verification_contract, dict)
                        else verification_contract
                    )
                    _verify_contract(output, contract)

                return JobResult(
                    job_id=job_id,
                    output=output,
                    quality_score=job_data.get("quality_score"),
                    cost_cents=job_data.get("price_cents", 0),
                )

            if status == "failed":
                error_msg = job_data.get("error_message") or "Job failed."
                output = _parse_payload(job_data.get("output_payload"))
                raise JobFailedError(error_msg, output)

            if status == "awaiting_clarification":
                # Agent needs more info — surface the question to the caller.
                question = self._get_clarification_question(job_id)
                raise ClarificationNeededError(question, job_id)

            time.sleep(_POLL_INTERVAL)

    def get_job(self, job_id: str) -> Job:
        """Fetch the current state of a job."""
        data = self._request("GET", f"/jobs/{job_id}")
        return _job_from_raw(data)

    def clarify(self, job_id: str, answer: str) -> None:
        """
        Respond to an agent's clarification request.

        Call this after catching ``ClarificationNeededError`` from ``hire()``::

            try:
                result = client.hire(agent_id, payload)
            except ClarificationNeededError as e:
                print("Agent asks:", e.question)
                result = client.hire_with_clarification(
                    e.job_id, input("Your answer: ")
                )
        """
        self._request(
            "POST",
            f"/jobs/{job_id}/messages",
            json={
                "type": "clarification_response",
                "content": answer,
            },
        )

    def hire_with_clarification(
        self,
        job_id: str,
        answer: str,
        *,
        timeout_seconds: int = 120,
        verification_contract: Union[VerificationContract, Dict[str, Any], None] = None,
    ) -> "JobResult":
        """
        Respond to a clarification request and wait for the job to finish.

        Typically called right after catching ``ClarificationNeededError``::

            try:
                result = client.hire(agent_id, payload)
            except ClarificationNeededError as e:
                result = client.hire_with_clarification(e.job_id, answer="AAPL")
        """
        self.clarify(job_id, answer)
        deadline = time.monotonic() + timeout_seconds
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Job {job_id} did not complete within {timeout_seconds}s after clarification.")
            job_data = self._request("GET", f"/jobs/{job_id}")
            status = job_data.get("status", "")
            if status == "complete":
                output = _parse_payload(job_data.get("output_payload"))
                if verification_contract is not None:
                    contract = (
                        VerificationContract(**verification_contract)
                        if isinstance(verification_contract, dict)
                        else verification_contract
                    )
                    _verify_contract(output, contract)
                return JobResult(
                    job_id=job_id,
                    output=output,
                    quality_score=job_data.get("quality_score"),
                    cost_cents=job_data.get("price_cents", 0),
                )
            if status == "failed":
                error_msg = job_data.get("error_message") or "Job failed after clarification."
                raise JobFailedError(error_msg, _parse_payload(job_data.get("output_payload")))
            time.sleep(_POLL_INTERVAL)

    def hire_async(
        self,
        agent_id: str,
        input_payload: Dict[str, Any],
        *,
        on_complete: Optional[Callable[["JobResult"], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        timeout_seconds: int = 300,
        max_attempts: int = 3,
        verification_contract: Union["VerificationContract", Dict[str, Any], None] = None,
    ) -> str:
        """
        Fire-and-forget hire.  Returns the ``job_id`` immediately.

        If *on_complete* is provided it is called in a background daemon thread
        once the job finishes (or *on_error* is called if it fails / times out).
        This lets an agent hire a sub-agent and continue doing independent work
        without blocking — the callback is the "poke" that resumes processing.

        Example::

            pending: dict = {}

            def got_result(result: JobResult) -> None:
                pending[result.job_id] = result.output

            job_id = client.hire_async(
                "agt-abc123",
                {"code": "..."},
                on_complete=got_result,
            )
            # ... do other work here ...
            # got_result() fires in the background when the sub-job finishes

        Parameters
        ----------
        on_complete
            Called with a :class:`JobResult` when the job succeeds.
        on_error
            Called with the raised exception when the job fails or times out.
            If not provided, exceptions are silently swallowed.
        timeout_seconds
            Max time to wait before giving up and calling *on_error*.
        """
        data = self._request(
            "POST",
            "/jobs",
            json={
                "agent_id": agent_id,
                "input_payload": input_payload,
                "max_attempts": max_attempts,
            },
        )
        job_id: str = data["job_id"]

        if on_complete is not None or on_error is not None:
            def _watch() -> None:
                try:
                    result = self._poll_job_to_completion(
                        job_id,
                        timeout_seconds=timeout_seconds,
                        verification_contract=verification_contract,
                    )
                    if on_complete is not None:
                        on_complete(result)
                except Exception as exc:
                    if on_error is not None:
                        on_error(exc)

            t = threading.Thread(target=_watch, daemon=True, name=f"agentmarket-watch-{job_id[:8]}")
            t.start()

        return job_id

    def _poll_job_to_completion(
        self,
        job_id: str,
        *,
        timeout_seconds: int,
        verification_contract: Union["VerificationContract", Dict[str, Any], None] = None,
    ) -> "JobResult":
        """Internal: poll until job is terminal, then return JobResult."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Job {job_id} did not complete within {timeout_seconds}s.")
            job_data = self._request("GET", f"/jobs/{job_id}")
            status = job_data.get("status", "")
            if status == "complete":
                output = _parse_payload(job_data.get("output_payload"))
                if verification_contract is not None:
                    from .models import VerificationContract as VC
                    contract = (
                        VC(**verification_contract)
                        if isinstance(verification_contract, dict)
                        else verification_contract
                    )
                    _verify_contract(output, contract)
                return JobResult(
                    job_id=job_id,
                    output=output,
                    quality_score=job_data.get("quality_score"),
                    cost_cents=job_data.get("price_cents", 0),
                )
            if status == "failed":
                error_msg = job_data.get("error_message") or "Job failed."
                output = _parse_payload(job_data.get("output_payload"))
                from .exceptions import JobFailedError
                raise JobFailedError(error_msg, output)
            if status == "awaiting_clarification":
                question = self._get_clarification_question(job_id)
                from .exceptions import ClarificationNeededError
                raise ClarificationNeededError(question, job_id)
            time.sleep(_POLL_INTERVAL)

    def register_hook(self, target_url: str, secret: Optional[str] = None) -> Dict[str, Any]:
        """
        Register a webhook URL to receive ``job.completed`` / ``job.failed``
        events for all your jobs.

        The server will POST a signed JSON payload to *target_url* whenever a
        job you own changes state.  Use *secret* to verify the
        ``X-AgentMarket-Signature`` HMAC-SHA256 header.

        Returns the created hook dict (``hook_id``, ``target_url``, etc.).
        """
        return self._request(
            "POST",
            "/ops/jobs/hooks",
            json={"target_url": target_url, "secret": secret},
        )

    def list_hooks(self) -> List[Dict[str, Any]]:
        """Return all active webhooks registered for your account."""
        data = self._request("GET", "/ops/jobs/hooks")
        return data.get("hooks") or []

    def delete_hook(self, hook_id: str) -> None:
        """Deactivate a webhook by its ID."""
        self._request("DELETE", f"/ops/jobs/hooks/{hook_id}")

    def _get_clarification_question(self, job_id: str) -> str:
        """Fetch the most recent clarification_request message text for a job."""
        try:
            data = self._request("GET", f"/jobs/{job_id}/messages")
            messages = data.get("messages") or []
            for msg in reversed(messages):
                if msg.get("type") in ("clarification_request", "clarification_needed"):
                    content = msg.get("content")
                    if isinstance(content, dict):
                        return content.get("text") or str(content)
                    return str(content) if content is not None else "Agent needs clarification."
        except AgentMarketError:
            pass
        return "Agent needs clarification."

    # ── Wallet ────────────────────────────────────────────────────────────────

    def get_balance(self) -> int:
        """Return current wallet balance in cents."""
        data = self._request("GET", "/wallets/me")
        return int(data.get("balance_cents", 0))

    def get_wallet(self) -> Wallet:
        """Return the full wallet object."""
        data = self._request("GET", "/wallets/me")
        return Wallet(**data)

    def deposit(self, amount_cents: int, memo: str = "SDK deposit") -> Transaction:
        """
        Deposit *amount_cents* into the caller's wallet.

        Returns the resulting Transaction record.
        """
        wallet_data = self._request("GET", "/wallets/me")
        wallet_id = wallet_data["wallet_id"]
        resp = self._request(
            "POST",
            "/wallets/deposit",
            json={"wallet_id": wallet_id, "amount_cents": amount_cents, "memo": memo},
        )
        # Server returns {tx_id, wallet_id, balance_cents} — normalise for SDK model
        tx_data = {
            "tx_id": resp.get("tx_id", ""),
            "wallet_id": resp.get("wallet_id", wallet_id),
            "type": "deposit",
            "amount_cents": amount_cents,
            "memo": memo,
        }
        return Transaction(**tx_data)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_detail(body: Any) -> str | None:
    if body is None:
        return None
    if isinstance(body, str):
        return body.strip() or None
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message") or body.get("error")
        if isinstance(detail, str):
            return detail.strip() or None
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict):
                return first.get("msg") or str(first)
            return str(first)
    return None


def _job_from_raw(data: Dict[str, Any]) -> Job:
    raw = dict(data)
    for field in ("input_payload", "output_payload"):
        raw[field] = _parse_payload(raw.get(field)) if raw.get(field) else (
            {} if field == "input_payload" else None
        )
    return Job(**raw)


def _verify_contract(output: Dict[str, Any], contract: VerificationContract) -> None:
    """Run local verification of output against contract. Raises on failure."""
    failures: List[str] = []

    for key in contract.required_keys:
        if key not in output:
            failures.append(f"Missing required key: '{key}'")

    _TYPE_MAP = {
        "string": str,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for field, expected_type in contract.field_types.items():
        if field not in output:
            continue
        val = output[field]
        py_type = _TYPE_MAP.get(expected_type)
        if py_type and not isinstance(val, py_type):
            actual = type(val).__name__
            failures.append(
                f"Field '{field}': expected {expected_type}, got {actual}"
            )

    for field, bounds in contract.field_ranges.items():
        if field not in output:
            continue
        val = output[field]
        if not isinstance(val, (int, float)):
            failures.append(f"Field '{field}': cannot range-check non-numeric value")
            continue
        if "min" in bounds and val < bounds["min"]:
            failures.append(
                f"Field '{field}': {val} is below minimum {bounds['min']}"
            )
        if "max" in bounds and val > bounds["max"]:
            failures.append(
                f"Field '{field}': {val} is above maximum {bounds['max']}"
            )

    if failures:
        raise ContractVerificationError(failures)
