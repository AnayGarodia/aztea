from __future__ import annotations

import re
import threading
import uuid
import warnings
from typing import Any, Iterable, cast

import requests

from ..errors import (
    AgentNotFoundError,
    AzteaError,
    raise_for_error_response,
)
from ..jobs import JobsNamespace
from ..models import Agent, Job as JobRecord, JobResult, Transaction, VerificationContract, Wallet
from ..types import JSONObject, JSONValue
from ..workers import JobSource, build_worker_decorator
from ._helpers import _coerce_model, _coerce_payload, _ensure_object
from ._polling import poll_job_to_completion
from ._verify import verify_job as _verify_job_impl
from .namespaces import (
    AgentsNamespace,
    AuthNamespace,
    DisputesNamespace,
    RegistryNamespace,
    WalletsNamespace,
)

# PEP 702 `typing.deprecated` lands in Python 3.13. On 3.9–3.12 we fall back
# to a no-op decorator (the runtime DeprecationWarning emitted from within
# the deprecated method still surfaces — this only affects static-analyzer
# / IDE warnings). /review caught the missing decorator 2026-05-27.
try:
    from typing import deprecated as _deprecated_decorator  # type: ignore[attr-defined]
except ImportError:
    def _deprecated_decorator(message: str, /):  # type: ignore[misc]
        def _wrap(fn):
            return fn
        return _wrap


class AzteaClient:
    """Synchronous HTTP client for the Aztea platform.

    Exception contract
    ------------------
    Every public method that hits the API can raise one of the following on a
    non-2xx response (see :mod:`aztea.errors` for the full hierarchy — all of
    these inherit from :class:`APIError`, which inherits from
    :class:`AzteaError`):

    - :class:`UnauthorizedError` (401) — missing/invalid/revoked API key.
    - :class:`InsufficientBalanceError` (402) — wallet balance below the
      required charge (also re-exported as ``InsufficientFundsError``).
    - :class:`ForbiddenError` (403) — key valid but lacks the required scope.
    - :class:`NotFoundError` (404) — agent/job/pipeline id unknown.
    - :class:`ConflictError` (409) — state-machine conflict (e.g. already
      rated, already disputed). :class:`ClaimLostError` for claim races.
    - :class:`UnprocessableEntityError` (422) — payload failed validation.
    - :class:`RateLimitError` (429) — caller is rate-limited. The hint
      surfaces ``Retry-After`` seconds when the server sets the header.
    - :class:`APIError` — any other 4xx/5xx response.

    Non-HTTP failures (timeouts, malformed JSON, missing fields the server
    is contractually required to send) raise :class:`AzteaError` directly.

    Methods that wait for terminal state — ``hire``,
    ``hire_with_clarification``, ``wait_for``, batch methods with
    ``wait=True`` — additionally raise:

    - :class:`JobTimeoutError` — ``timeout_seconds`` elapsed before the job
      reached a terminal state.
    - :class:`JobFailedError` — the job reached the ``failed`` terminal state.
    - :class:`ClarificationNeededError` — the job is blocked on caller input
      (only when called via :meth:`hire_with_clarification`).
    - :class:`AzteaJobStoppedError` — co-pilot mode ``stop_when`` matched a
      partial output and aborted the job.
    - :class:`ContractVerificationError` — output failed
      ``verification_contract`` validation.

    Methods that diverge from the baseline call this out in their own
    ``Raises:`` section.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        client_id: str = "aztea-python-sdk",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._api_key = api_key
        self._client_id = str(client_id or "aztea-python-sdk").strip() or "aztea-python-sdk"
        self._session = requests.Session()

        self.auth = AuthNamespace(self)
        self.wallets = WalletsNamespace(self)
        self.registry = RegistryNamespace(self)
        self.jobs = JobsNamespace(self)
        self.disputes = DisputesNamespace(self)
        # Wave 2 (2026-05-26): high-level surface mirroring the TypeScript SDK
        # shape (`client.agents.*`). Legacy `client.hire()` delegates here and
        # emits a DeprecationWarning. See AgentsNamespace.
        self.agents = AgentsNamespace(self)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "AzteaClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def set_api_key(self, api_key: str | None) -> None:
        self._api_key = api_key

    def worker(
        self,
        agent_id: str,
        *,
        concurrency: int = 1,
        lease_seconds: int = 300,
        poll_interval: float = 2.0,
        job_source: JobSource | None = None,
    ) -> Any:
        return build_worker_decorator(
            self,
            agent_id=agent_id,
            concurrency=concurrency,
            lease_seconds=lease_seconds,
            poll_interval=poll_interval,
            job_source=job_source,
        )

    def _headers(self, *, require_api_key: bool = True) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Aztea-Version": "1.0",
            "X-Aztea-Client": self._client_id,
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif require_api_key:
            raise AzteaError("This operation requires an API key.")
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: JSONObject | None = None,
        require_api_key: bool = True,
        timeout: float | None = None,
        stream: bool = False,
    ) -> requests.Response:
        response = self._session.request(
            method=method,
            url=f"{self.base_url}{path}",
            params=params,
            json=json_body,
            headers=self._headers(require_api_key=require_api_key),
            timeout=self.timeout if timeout is None else timeout,
            stream=stream,
        )
        raise_for_error_response(response)
        return response

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: JSONObject | None = None,
        require_api_key: bool = True,
        timeout: float | None = None,
    ) -> JSONObject:
        response = self._request(
            method,
            path,
            params=params,
            json_body=json_body,
            require_api_key=require_api_key,
            timeout=timeout,
        )
        parsed = response.json()
        return _ensure_object(parsed, context=f"{method} {path}")

    def _stream(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        require_api_key: bool = True,
    ) -> requests.Response:
        return self._request(
            method,
            path,
            params=params,
            require_api_key=require_api_key,
            timeout=None,
            stream=True,
        )

    # High-level compatibility surface

    @staticmethod
    def _slugify_agent_name(name: str) -> str:
        return re.sub(r"(^-+|-+$)", "", re.sub(r"[^a-z0-9]+", "-", name.lower()))

    def _resolve_agent_reference(self, agent_ref: str) -> str:
        """Resolve UUIDs, slugs, and display names before creating jobs."""
        raw = str(agent_ref or "").strip()
        try:
            uuid.UUID(raw)
            return raw
        except ValueError:
            pass
        wanted = raw.lower()
        try:
            agents = self.list_agents()
        except (AzteaError, requests.RequestException):
            return raw
        for agent in agents:
            name = str(getattr(agent, "name", "") or "")
            if str(getattr(agent, "agent_id", "") or "").lower() == wanted:
                return str(agent.agent_id)
            if str(getattr(agent, "slug", "") or "").lower() == wanted:
                return str(agent.agent_id)
            if self._slugify_agent_name(name) == wanted or name.lower() == wanted:
                return str(agent.agent_id)
        raise AgentNotFoundError(agent_id=agent_ref, message=f"Unknown agent '{agent_ref}'.")

    def search_agents(
        self,
        query: str,
        *,
        max_price_cents: int | None = None,
        min_trust: float | None = None,
    ) -> list[Agent]:
        data = self.registry.search(str(query).strip())
        raw_results = data.get("results") or []
        agents = [
            _coerce_model(Agent, item["agent"])
            for item in raw_results
            if isinstance(item, dict) and isinstance(item.get("agent"), dict)
        ]
        if max_price_cents is not None:
            agents = [agent for agent in agents if agent.price_cents <= max_price_cents]
        if min_trust is not None:
            agents = [agent for agent in agents if agent.trust_score >= min_trust]
        return agents

    def list_agents(self, *, tag: str | None = None, rank_by: str = "trust") -> list[Agent]:
        data = self.registry.list(tag=tag, rank_by=rank_by)
        raw_agents = data.get("agents") or []
        return [_coerce_model(Agent, item) for item in raw_agents if isinstance(item, dict)]

    def get_agent(self, agent_id: str) -> Agent:
        raw = self.registry.get(agent_id)
        try:
            return _coerce_model(Agent, raw)
        except TypeError as exc:
            raise AgentNotFoundError(agent_id=agent_id, message=str(exc)) from exc

    def get_balance(self) -> int:
        return int(self.wallets.me().get("balance_cents") or 0)

    def get_wallet(self) -> Wallet:
        return _coerce_model(Wallet, self.wallets.me())

    def get_job_full_output(self, job_id: str) -> JSONObject:
        return self._request_json("GET", f"/jobs/{job_id}/full")

    def deposit(self, amount_cents: int, memo: str = "SDK deposit") -> Transaction:
        wallet = self.wallets.me()
        raw = self.wallets.deposit(str(wallet["wallet_id"]), amount_cents, memo=memo)
        if "tx_id" in raw:
            return Transaction(
                tx_id=str(raw.get("tx_id") or ""),
                wallet_id=str(raw.get("wallet_id") or wallet["wallet_id"]),
                type=str(raw.get("type") or "deposit"),
                amount_cents=int(raw.get("amount_cents") or amount_cents),
                memo=str(raw.get("memo") or memo),
                agent_id=raw.get("agent_id"),
                created_at=str(raw.get("created_at") or ""),
            )
        return Transaction(
            tx_id="",
            wallet_id=str(wallet["wallet_id"]),
            type="deposit",
            amount_cents=amount_cents,
            memo=memo,
        )

    def get_spend_summary(self, period: str = "7d") -> JSONObject:
        return self._request_json("GET", "/wallets/spend-summary", params={"period": period})

    def create_topup_session(self, amount_cents: int, wallet_id: str | None = None) -> JSONObject:
        if wallet_id is None:
            wallet = self.wallets.me()
            wallet_id = str(wallet["wallet_id"])
        return self._request_json(
            "POST",
            "/wallets/topup/session",
            json_body={"wallet_id": wallet_id, "amount_cents": int(amount_cents)},
        )

    def list_pipelines(self) -> JSONObject:
        return self._request_json("GET", "/pipelines")

    def get_pipeline(self, pipeline_id: str) -> JSONObject:
        return self._request_json("GET", f"/pipelines/{pipeline_id}")

    def run_pipeline(self, pipeline_id: str, input_payload: JSONObject) -> JSONObject:
        return self._request_json("POST", f"/pipelines/{pipeline_id}/run", json_body={"input_payload": input_payload})

    def get_pipeline_run(self, pipeline_id: str, run_id: str) -> JSONObject:
        return self._request_json("GET", f"/pipelines/{pipeline_id}/runs/{run_id}")

    def get_pipeline_run_by_id(self, run_id: str) -> JSONObject:
        """Look up a pipeline run by run_id alone — the server resolves
        pipeline_id from it. Convenience for callers that only retained
        run_id (e.g. after a recipe execution returned just {run_id, status}).
        """
        return self._request_json("GET", f"/pipelines/runs/{run_id}")

    def list_recipes(self) -> JSONObject:
        return self._request_json("GET", "/recipes")

    def run_recipe(self, recipe_id: str, input_payload: JSONObject) -> JSONObject:
        return self._request_json("POST", f"/recipes/{recipe_id}/run", json_body={"input_payload": input_payload})

    def get_job(self, job_id: str) -> JobRecord:
        job = _coerce_model(JobRecord, self.jobs.get_raw(job_id))
        return job.bind_client(self)

    @_deprecated_decorator(
        "AzteaClient.hire() is deprecated; use client.agents.call() instead. "
        "Scheduled for removal in the 2.0 release."
    )
    def hire(
        self,
        agent_id: str,
        input_payload: dict[str, Any],
        *,
        verification_contract: VerificationContract | dict[str, Any] | None = None,
        wait: bool = True,
        timeout_seconds: int = 60,
        max_attempts: int = 3,
        budget_cents: int | None = None,
        max_price_cents: int | None = None,
        callback_url: str | None = None,
        callback_secret: str | None = None,
        parent_job_id: str | None = None,
        parent_cascade_policy: str = "detach",
        clarification_timeout_seconds: int | None = None,
        clarification_timeout_policy: str = "fail",
        output_verification_window_seconds: int | None = None,
    ) -> JobResult:
        """Deprecated alias for :meth:`agents.call`.

        Kept indefinitely for backward compatibility with existing code that
        calls `client.hire(agent_id, payload)`. The Wave 2 (2026-05-26)
        platform pivot renamed the preferred surface to
        `client.agents.call(name_or_id, payload)` to align with the TypeScript
        SDK and remove the "specialist marketplace" framing in favor of the
        platform identity. Same kwargs, same return, same exception set —
        only the name changed.

        Emits a `DeprecationWarning` on every call so the call sites surface
        in tooling; suppress with `warnings.filterwarnings(...)` if you
        deliberately want to keep using `hire()`.
        """
        warnings.warn(
            "AzteaClient.hire() is deprecated and will be removed in a future "
            "major release. Use client.agents.call(name_or_id, payload) instead. "
            "Same signature, same return, same exceptions — only the name changed.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._call_agent_impl(
            agent_id,
            input_payload,
            verification_contract=verification_contract,
            wait=wait,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            budget_cents=budget_cents,
            max_price_cents=max_price_cents,
            callback_url=callback_url,
            callback_secret=callback_secret,
            parent_job_id=parent_job_id,
            parent_cascade_policy=parent_cascade_policy,
            clarification_timeout_seconds=clarification_timeout_seconds,
            clarification_timeout_policy=clarification_timeout_policy,
            output_verification_window_seconds=output_verification_window_seconds,
        )

    def _call_agent_impl(
        self,
        agent_id: str,
        input_payload: dict[str, Any],
        *,
        verification_contract: VerificationContract | dict[str, Any] | None = None,
        wait: bool = True,
        timeout_seconds: int = 60,
        max_attempts: int = 3,
        budget_cents: int | None = None,
        max_price_cents: int | None = None,
        callback_url: str | None = None,
        callback_secret: str | None = None,
        parent_job_id: str | None = None,
        parent_cascade_policy: str = "detach",
        clarification_timeout_seconds: int | None = None,
        clarification_timeout_policy: str = "fail",
        output_verification_window_seconds: int | None = None,
    ) -> JobResult:
        """Internal: create a job and (by default) wait for terminal state.

        The real implementation. Public callers go through `agents.call()`
        (preferred) or `hire()` (deprecated). The deprecation warning sits
        in `hire()` so `agents.call()` does not retrigger it on every job.

        With ``wait=True`` (the default) this polls until the job completes,
        fails, times out, or is stopped — and resolves the corresponding
        :class:`JobResult` or raises one of the terminal-state exceptions
        listed below. With ``wait=False``, the call returns immediately after
        the server accepts the job; the caller is responsible for polling
        (e.g. via :meth:`wait_for`).

        ``budget_cents`` / ``max_price_cents`` cap the charge the wallet will
        accept; the server enforces both, so a price overrun raises
        :class:`InsufficientBalanceError` rather than silently overcharging.

        Raises:
            InsufficientBalanceError: wallet balance is below the agent's
                ``price_per_call`` (or below ``budget_cents`` if set).
            JobTimeoutError: ``wait=True`` and ``timeout_seconds`` elapsed
                before the job reached a terminal state.
            JobFailedError: the job reached the ``failed`` state.
            AzteaJobStoppedError: co-pilot ``stop_when`` matched a partial
                output and aborted the job.
            ContractVerificationError: ``verification_contract`` was set
                and the agent's output failed validation.
            AzteaError: the server response was missing a required field
                (e.g. ``job_id``).

        See the class-level docstring for the baseline HTTP exception set
        (401/402/403/404/422/429 etc.) which can also be raised here.
        """
        resolved_agent_id = self._resolve_agent_reference(agent_id)
        body: JSONObject = {
            "agent_id": resolved_agent_id,
            "input_payload": cast(JSONValue, input_payload),
            "max_attempts": max_attempts,
            "parent_cascade_policy": parent_cascade_policy,
            "clarification_timeout_policy": clarification_timeout_policy,
        }
        if budget_cents is not None:
            body["budget_cents"] = budget_cents
        if max_price_cents is not None:
            body["max_price_cents"] = max_price_cents
        if callback_url is not None:
            body["callback_url"] = callback_url
        if callback_secret is not None:
            body["callback_secret"] = callback_secret
        if parent_job_id is not None:
            body["parent_job_id"] = parent_job_id
        if clarification_timeout_seconds is not None:
            body["clarification_timeout_seconds"] = clarification_timeout_seconds
        if output_verification_window_seconds is not None:
            body["output_verification_window_seconds"] = output_verification_window_seconds
        created = self._request_json("POST", "/jobs", json_body=body)
        raw_job_id = created.get("job_id")
        if not isinstance(raw_job_id, str) or not raw_job_id.strip():
            raise AzteaError("POST /jobs response is missing a valid job_id.")
        if not wait:
            return JobResult(
                job_id=raw_job_id,
                output={},
                cost_cents=int(created.get("price_cents") or 0),
            ).bind_client(self)
        return self._poll_job_to_completion(
            raw_job_id,
            timeout_seconds=timeout_seconds,
            verification_contract=verification_contract,
        )

    def wait_for(self, job_id: str, timeout_seconds: int = 60) -> JobResult:
        """Poll an existing job until it reaches a terminal state.

        Use this after :meth:`hire` with ``wait=False`` or
        :meth:`hire_async`. Polls at the server's recommended cadence and
        resolves to a :class:`JobResult` on success, or raises one of the
        terminal-state exceptions below.

        Raises:
            JobTimeoutError: ``timeout_seconds`` elapsed before the job
                reached a terminal state.
            JobFailedError: the job reached the ``failed`` state.
            AzteaJobStoppedError: co-pilot ``stop_when`` matched a partial
                output and aborted the job.
            NotFoundError: ``job_id`` doesn't exist or isn't visible to this
                caller.
        """
        return self._poll_job_to_completion(job_id, timeout_seconds=timeout_seconds)

    def hire_many(
        self,
        specs: list[dict[str, Any]],
        *,
        wait: bool = False,
        timeout_seconds: int = 300,
    ) -> list[JobResult]:
        data = self._request_json("POST", "/jobs/batch", json_body={"jobs": cast(JSONValue, specs)})
        raw_jobs = data.get("jobs") or []
        results: list[JobResult] = []
        for index, entry in enumerate(raw_jobs):
            if not isinstance(entry, dict):
                raise AzteaError(f"POST /jobs/batch jobs[{index}] expected an object response.")
            job_id = entry.get("job_id")
            if not isinstance(job_id, str) or not job_id.strip():
                raise AzteaError(f"POST /jobs/batch jobs[{index}] missing a valid job_id.")
            results.append(
                JobResult(
                    job_id=job_id,
                    output=_coerce_payload(entry.get("output_payload")),
                    cost_cents=int(entry.get("price_cents") or 0),
                ).bind_client(self)
            )
        if not wait:
            return results
        return [self._poll_job_to_completion(item.job_id, timeout_seconds=timeout_seconds) for item in results]

    def hire_batch(
        self,
        specs: list[dict[str, Any]],
        *,
        intent: str | None = None,
        max_total_cents: int | None = None,
        dry_run: bool = False,
    ) -> JSONObject:
        """Submit independent jobs as one parallel marketplace hire.

        Unlike :meth:`hire_many`, this returns the full batch rail response:
        ``batch_id``, ``job_ids``, ``total_charged_cents``,
        ``marketplace_transaction``, and ``parallel_hire_trace``. Use this
        when the caller needs to show escrow, settlement, and receipt state
        for the batch instead of only individual job handles.

        Raises:
            InsufficientBalanceError: aggregate cost exceeds wallet balance
                or ``max_total_cents`` (the server settles the cap before
                claiming any of the jobs, so the batch is all-or-nothing).
            UnprocessableEntityError: one or more ``specs`` failed
                per-job validation (the response details enumerate which).
        """
        body: JSONObject = {"jobs": cast(JSONValue, specs)}
        if intent is not None:
            body["intent"] = str(intent)
        if max_total_cents is not None:
            body["max_total_cents"] = int(max_total_cents)
        if dry_run:
            body["dry_run"] = True
        return self._request_json("POST", "/jobs/batch", json_body=body)

    def get_batch(self, batch_id: str, *, include: str | None = None) -> JSONObject:
        """Fetch aggregate status for a parallel marketplace hire."""
        path = f"/jobs/batch/{batch_id}"
        if include:
            path = f"{path}?include={include}"
        return self._request_json("GET", path)

    def decide_output_verification(
        self,
        job_id: str,
        *,
        decision: str,
        reason: str | None = None,
        evidence: str | None = None,
    ) -> JobRecord:
        body: JSONObject = {"decision": decision}
        if reason is not None:
            body["reason"] = reason
        if evidence is not None:
            body["evidence"] = evidence
        return _coerce_model(JobRecord, self._request_json("POST", f"/jobs/{job_id}/verification", json_body=body))

    def clarify(self, job_id: str, answer: str, *, request_message_id: int | None = None) -> JSONObject:
        payload: JSONObject = {"answer": answer}
        if request_message_id is None:
            messages = self.jobs.list_messages(job_id).get("messages") or []
            latest = next(
                (
                    msg for msg in reversed(messages)
                    if isinstance(msg, dict)
                    and msg.get("type") == "clarification_request"
                    and isinstance(msg.get("message_id"), int)
                ),
                None,
            )
            if latest is not None:
                request_message_id = latest["message_id"]
        if request_message_id is not None:
            payload["request_message_id"] = request_message_id
        return self.jobs.post_message(job_id, "clarification_response", payload)

    def hire_with_clarification(
        self,
        job_id: str,
        answer: str,
        *,
        timeout_seconds: int = 120,
        verification_contract: VerificationContract | dict[str, Any] | None = None,
    ) -> JobResult:
        """Answer a clarification request, then resume polling to terminal state.

        Use after a :class:`ClarificationNeededError` was raised mid-poll (or
        the agent sent a ``clarification_request`` message); this posts the
        answer and continues waiting on the same job. If the agent asks a
        second clarification, this raises again — the caller is expected to
        loop until the job completes or fails.

        Raises:
            ClarificationNeededError: the agent emitted another
                ``clarification_request`` before reaching terminal state.
            JobTimeoutError: ``timeout_seconds`` elapsed before resolution.
            JobFailedError: the job reached ``failed``.
            ContractVerificationError: output failed contract validation.
        """
        self.clarify(job_id, answer)
        return self._poll_job_to_completion(
            job_id,
            timeout_seconds=timeout_seconds,
            verification_contract=verification_contract,
        )

    def hire_async(
        self,
        agent_id: str,
        input_payload: dict[str, Any],
        *,
        on_complete: Any | None = None,
        on_error: Any | None = None,
        timeout_seconds: int = 300,
        verification_contract: VerificationContract | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Fire-and-forget hire — returns the job_id once the server accepts it.

        Optionally watches the job in a daemon thread and invokes
        ``on_complete(JobResult)`` or ``on_error(exc)`` when it terminates.
        The thread is detached — exceptions inside it can't propagate to the
        caller; that's why ``on_error`` exists. The synchronous portion (the
        ``POST /jobs`` itself) can raise the baseline HTTP exception set;
        any failure during background polling is delivered via ``on_error``,
        not raised.

        Raises:
            InsufficientBalanceError: wallet can't cover the listed price.
            UnprocessableEntityError: ``input_payload`` failed validation.
            AzteaError: the server response was missing a valid job_id.
        """
        result = self.hire(agent_id, input_payload, wait=False, **kwargs)
        if on_complete is not None or on_error is not None:
            def _watch() -> None:
                try:
                    completed = self._poll_job_to_completion(
                        result.job_id,
                        timeout_seconds=timeout_seconds,
                        verification_contract=verification_contract,
                    )
                    if on_complete is not None:
                        on_complete(completed)
                except Exception as exc:
                    if on_error is not None:
                        on_error(exc)

            threading.Thread(target=_watch, daemon=True, name=f"aztea-watch-{result.job_id[:8]}").start()
        return result.job_id

    def cancel_job(self, job_id: str, *, reason: str | None = None) -> JSONObject:
        """Abort an in-flight async job and refund the unsettled charge.

        Terminal-state jobs raise ConflictError(409, error="job.invalid_state").
        """
        body: JSONObject = {}
        if reason:
            body["reason"] = str(reason)[:200]
        return self._request_json("POST", f"/jobs/{job_id}/cancel", json_body=body)

    def rate_job(self, job_id: str, rating: int) -> JSONObject:
        """Submit a 1–5 star rating after a completed job.

        Ratings feed into trust scoring + payout-curve clawback for the agent.
        """
        return self._request_json("POST", f"/jobs/{job_id}/rating", json_body={"rating": int(rating)})

    def rate_caller(self, job_id: str, rating: int, comment: str | None = None) -> JSONObject:
        """Agent-side bilateral rating: rate the caller after completing a job."""
        body: JSONObject = {"rating": int(rating)}
        if comment is not None:
            body["comment"] = str(comment)
        return self._request_json("POST", f"/jobs/{job_id}/rate-caller", json_body=body)

    def compare(
        self,
        agent_ids: list[str] | None,
        input_payload: JSONObject,
        *,
        slugs: list[str] | None = None,
        max_cost_usd: float | None = None,
    ) -> JSONObject:
        """Run the same task across multiple agents in parallel for side-by-side comparison."""
        if not agent_ids and not slugs:
            raise AzteaError("compare requires agent_ids or slugs.")
        body: JSONObject = {"input_payload": input_payload}
        if agent_ids:
            body["agent_ids"] = cast(JSONValue, list(agent_ids))
        if slugs:
            body["slugs"] = cast(JSONValue, list(slugs))
        if max_cost_usd is not None:
            body["max_cost_usd"] = float(max_cost_usd)
        # The server route is /jobs/compare (POST); the older SDK URL
        # /registry/agents/compare returned 404. Cross-surface parity fix.
        return self._request_json("POST", "/jobs/compare", json_body=body)

    def auto_hire(
        self,
        intent: str,
        *,
        input_payload: JSONObject | None = None,
        max_cost_usd: float | None = None,
        dry_run: bool = False,
        output_format: str | None = None,
    ) -> JSONObject:
        """Pick the best agent for a natural-language intent and run it under hard cost gates.

        Unlike :meth:`hire`, the gates can short-circuit to a no-charge
        recommendation list (HTTP 200 with ``decision="recommend"``) when
        price / confidence / trust thresholds aren't met — that's a normal
        result, not an error. The exception surface is the baseline HTTP
        set; budget overruns surface as ``decision="recommend"`` instead of
        ``InsufficientBalanceError``.
        """
        body: JSONObject = {"intent": str(intent), "dry_run": bool(dry_run)}
        if input_payload is not None:
            body["input"] = input_payload
        if max_cost_usd is not None:
            body["max_cost_usd"] = float(max_cost_usd)
        if output_format is not None:
            body["output_format"] = str(output_format)
        return self._request_json("POST", "/registry/agents/auto-hire", json_body=body)

    def dispute_job(
        self,
        job_id: str,
        *,
        reason: str,
        evidence: str | None = None,
    ) -> JSONObject:
        """Open a dispute on a completed job. Triggers LLM-judge review.

        Raises:
            ConflictError: a dispute already exists, the job hasn't
                completed, or the dispute window has closed (specific code
                in ``err.code``: ``dispute.already_exists`` /
                ``dispute.invalid_state`` / ``dispute.window_closed``).
            InsufficientBalanceError: filing deposit exceeds wallet balance
                (``payment.insufficient_funds`` with the required cents in
                ``err.body.details``).
        """
        body: JSONObject = {"reason": str(reason)}
        if evidence is not None:
            body["evidence"] = str(evidence)
        return self._request_json("POST", f"/jobs/{job_id}/dispute", json_body=body)

    def get_dispute(self, job_id: str) -> JSONObject:
        return self._request_json("GET", f"/jobs/{job_id}/dispute")

    def retry_job(self, job_id: str) -> JSONObject:
        """Submit a fresh attempt for a previously-failed job (subject to max_attempts)."""
        return self._request_json("POST", f"/jobs/{job_id}/retry")

    def estimate_cost(self, agent_id: str, input_payload: JSONObject | None = None) -> JSONObject:
        """Preview the all-in caller charge for an agent before hiring.

        Returns price_cents, p50/p95 latency, confidence — same surface the
        MCP `aztea_estimate_cost` tool uses.
        """
        body = dict(input_payload or {})
        return self._request_json("POST", f"/agents/{agent_id}/estimate", json_body=body)

    def list_jobs(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        cursor: str | None = None,
    ) -> JSONObject:
        """Paginated list of the caller's jobs.

        ``status`` accepts a single status (``"complete"``) or a comma-list
        (``"complete,failed"``) — the latter lets the CLI dispute picker
        fetch only terminal jobs in one round-trip.
        """
        params: dict[str, str] = {"limit": str(int(limit))}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._request_json("GET", "/jobs", params=params)

    def get_dispute_policy(self) -> JSONObject:
        """Public read-only view of the dispute filing policy.

        Returns ``{filing_deposit_bps, filing_deposit_min_cents,
        default_dispute_window_hours, judges_required, judges_total, formula}``.
        Used by the CLI dispute wizard to quote the exact deposit amount
        before the user confirms. No auth required.
        """
        return self._request_json(
            "GET", "/ops/dispute-policy", require_api_key=False
        )

    # ─── Identity & verifiable receipts (the moat) ───────────────────────────

    def get_agent_did(self, agent_id: str) -> JSONObject:
        """Fetch an agent's published did:web document.

        Returns the W3C DID document with the agent's Ed25519 verification key.
        Treat the returned ``publicKeyMultibase`` as the public key buyers can
        use to verify any signed receipt from this agent.
        """
        return self._request_json("GET", f"/agents/{agent_id}/did.json", require_api_key=False)

    def get_job_signature(self, job_id: str) -> JSONObject:
        """Fetch the cryptographic receipt for a completed job.

        Returns ``{job_id, agent_did, output_hash, signature, signed_at}``.
        Pair with :meth:`verify_job` to check the signature locally without
        trusting Aztea.
        """
        return self._request_json("GET", f"/jobs/{job_id}/signature")

    def verify_job(self, job_id: str) -> JSONObject:
        """Fetch + verify a job's signed receipt against its agent's DID document.

        Returns ``{verified: bool, agent_did, signed_at, output_hash,
        verification_error?}``. ``verified=True`` means: the agent's published
        public key signed exactly this output payload at this timestamp. The
        platform cannot have tampered with the result without breaking the
        signature.

        This is the buyer-facing helper for the cryptographic-identity layer
        Aztea ships under ``did:web``. Earlier the primitives existed but no
        client surface called them — anyone can now ``verify_job(id)`` to get
        the same guarantee a third party would.
        """
        return _verify_job_impl(self, job_id)

    # ─── Stripe Connect: agent payouts to a real bank account ───────────────

    def get_connect_status(self) -> JSONObject:
        """Stripe Connect onboarding status for the authenticated user."""
        return self._request_json("GET", "/wallets/connect/status")

    def start_connect_onboarding(
        self,
        *,
        return_url: str | None = None,
        refresh_url: str | None = None,
    ) -> JSONObject:
        """Begin or resume Stripe Connect onboarding."""
        body: JSONObject = {}
        if return_url is not None:
            body["return_url"] = return_url
        if refresh_url is not None:
            body["refresh_url"] = refresh_url
        return self._request_json("POST", "/wallets/connect/onboard", json_body=body)

    def withdraw(self, amount_cents: int, *, memo: str | None = None) -> JSONObject:
        """Move ``amount_cents`` from the wallet balance to the connected Stripe account.

        Minimum $1.00 ($100 cents), maximum $10,000.00 ($1,000,000 cents).
        Requires a Connect account with charges_enabled=True.

        Raises:
            InsufficientBalanceError: wallet balance is below ``amount_cents``.
            ConflictError: Stripe Connect onboarding is incomplete or the
                account has ``charges_enabled=False`` — call
                :meth:`get_connect_status` to see why.
            UnprocessableEntityError: ``amount_cents`` is below the $1.00
                minimum or above the $10,000.00 cap.
        """
        body: JSONObject = {"amount_cents": int(amount_cents)}
        if memo is not None:
            body["memo"] = str(memo)
        return self._request_json("POST", "/wallets/withdraw", json_body=body)

    def list_withdrawals(self, *, limit: int = 25) -> JSONObject:
        return self._request_json(
            "GET", "/wallets/withdrawals", params={"limit": str(int(limit))}
        )

    # ─── Job event streaming (SSE) ───────────────────────────────────────────

    def stream_job(self, job_id: str, *, since: int | None = None) -> Iterable[JSONObject]:
        """Iterate over job events as they happen (Server-Sent Events).

        Thin convenience wrapper around ``client.jobs.stream_messages`` so the
        canonical SDK surface exposes streaming without dropping into the
        namespaced object. ``since`` is the last seen ``message_id``; pass
        ``None`` to receive all messages from the start of the job.
        """
        params: dict[str, str] = {}
        if since is not None:
            params["since"] = str(int(since))
        return self.jobs.stream_messages(job_id, since=since)

    # ─── Agent caller keys (A2A primitive) ───────────────────────────────────

    def create_agent_caller_key(
        self,
        agent_id: str,
        *,
        name: str = "agent caller key",
        label: str | None = None,
        scopes: list[str] | None = None,
    ) -> JSONObject:
        """Mint an ``azac_*`` caller key scoped to one of *your own* agents."""
        if label is not None:
            name = label
        body: JSONObject = {"name": name}
        if scopes:
            body["scopes"] = list(scopes)
        return self._request_json("POST", f"/registry/agents/{agent_id}/caller-keys", json_body=body)

    def list_agent_caller_keys(self, agent_id: str) -> JSONObject:
        return self._request_json("GET", f"/registry/agents/{agent_id}/keys")

    def register_hook(self, target_url: str, secret: str | None = None) -> JSONObject:
        return self._request_json("POST", "/ops/jobs/hooks", json_body={"target_url": target_url, "secret": secret})

    def list_hooks(self) -> list[dict[str, Any]]:
        raw = self._request_json("GET", "/ops/jobs/hooks")
        hooks = raw.get("hooks") or []
        return [item for item in hooks if isinstance(item, dict)]

    def delete_hook(self, hook_id: str) -> JSONObject:
        return self._request_json("DELETE", f"/ops/jobs/hooks/{hook_id}")

    def _poll_job_to_completion(
        self,
        job_id: str,
        *,
        timeout_seconds: int,
        verification_contract: VerificationContract | dict[str, Any] | None = None,
    ) -> JobResult:
        return poll_job_to_completion(
            self,
            job_id,
            timeout_seconds=timeout_seconds,
            verification_contract=verification_contract,
        )
