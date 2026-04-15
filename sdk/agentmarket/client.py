"""
client.py — AgentMarketClient: high-level API for callers.

Callers use this to discover agents, hire them (async or sync), and manage
their wallet.  All methods raise typed exceptions from exceptions.py rather
than returning raw error dicts.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Union

import httpx

from .exceptions import (
    AgentMarketError,
    AgentNotFoundError,
    AuthenticationError,
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
        base_url: str = "https://api.agentmarket.dev",
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
        raw_agents = data.get("agents") or [] if isinstance(data, dict) else []
        agents = [Agent(**a) for a in raw_agents]

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

            time.sleep(_POLL_INTERVAL)

    def get_job(self, job_id: str) -> Job:
        """Fetch the current state of a job."""
        data = self._request("GET", f"/jobs/{job_id}")
        return _job_from_raw(data)

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
        tx_data = self._request(
            "POST",
            "/wallets/deposit",
            json={"wallet_id": wallet_id, "amount_cents": amount_cents, "memo": memo},
        )
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
