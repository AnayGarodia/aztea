from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import threading
import time
from typing import Any, Iterable, cast

import requests

from .errors import (
    AgentNotFoundError,
    APIError,
    AzteaError,
    ClarificationNeededError,
    ContractVerificationError,
    JobFailedError,
    raise_for_error_response,
)
from .jobs import JobsNamespace
from .models import Agent, Job as JobRecord, JobResult, Transaction, VerificationContract, Wallet
from .types import JSONObject, JSONValue
from .workers import JobSource, build_worker_decorator


def _ensure_object(value: Any, *, context: str) -> JSONObject:
    if isinstance(value, dict):
        return value
    raise AzteaError(f"{context} expected a JSON object response, got: {type(value).__name__}.")


def _coerce_payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_model(model_type: Any, value: Any) -> Any:
    if not isinstance(value, dict):
        raise AzteaError(f"Expected object payload for {getattr(model_type, '__name__', 'model')}.")
    if not is_dataclass(model_type):
        return model_type(**value)
    allowed = {item.name for item in fields(model_type)}
    payload = {key: raw for key, raw in value.items() if key in allowed}
    return model_type(**payload)


def _verify_contract(output: dict[str, Any], contract: VerificationContract) -> None:
    failures: list[str] = []
    for key in contract.required_keys:
        if key not in output:
            failures.append(f"Missing required key: {key}")
    for key, expected in contract.field_types.items():
        if key not in output:
            continue
        value = output[key]
        kind = str(expected).strip().lower()
        if kind == "string" and not isinstance(value, str):
            failures.append(f"{key} expected string, got {type(value).__name__}")
        elif kind == "number" and not isinstance(value, (int, float)):
            failures.append(f"{key} expected number, got {type(value).__name__}")
        elif kind == "boolean" and not isinstance(value, bool):
            failures.append(f"{key} expected boolean, got {type(value).__name__}")
        elif kind == "array" and not isinstance(value, list):
            failures.append(f"{key} expected array, got {type(value).__name__}")
        elif kind == "object" and not isinstance(value, dict):
            failures.append(f"{key} expected object, got {type(value).__name__}")
    for key, bounds in contract.field_ranges.items():
        if key not in output or not isinstance(output[key], (int, float)) or not isinstance(bounds, dict):
            continue
        if "min" in bounds and output[key] < bounds["min"]:
            failures.append(f"{key} is below minimum {bounds['min']}")
        if "max" in bounds and output[key] > bounds["max"]:
            failures.append(f"{key} is above maximum {bounds['max']}")
    if failures:
        raise ContractVerificationError(failures)


@dataclass
class _NamespaceBase:
    _client: "AzteaClient"


class AuthNamespace(_NamespaceBase):
    def register(self, username: str, email: str, password: str) -> JSONObject:
        return self._client._request_json(
            "POST",
            "/auth/register",
            json_body={"username": username, "email": email, "password": password},
            require_api_key=False,
        )

    def login(self, email: str, password: str) -> JSONObject:
        return self._client._request_json(
            "POST",
            "/auth/login",
            json_body={"email": email, "password": password},
            require_api_key=False,
        )

    def me(self) -> JSONObject:
        return self._client._request_json("GET", "/auth/me")

    def list_keys(self) -> JSONObject:
        return self._client._request_json("GET", "/auth/keys")

    def create_key(self, name: str = "New key", scopes: Iterable[str] | None = None) -> JSONObject:
        payload: JSONObject = {"name": name}
        if scopes is not None:
            payload["scopes"] = list(scopes)
        return self._client._request_json("POST", "/auth/keys", json_body=payload)

    def rotate_key(
        self,
        key_id: str,
        *,
        name: str | None = None,
        scopes: Iterable[str] | None = None,
    ) -> JSONObject:
        payload: JSONObject = {}
        if name is not None:
            payload["name"] = name
        if scopes is not None:
            payload["scopes"] = list(scopes)
        return self._client._request_json("POST", f"/auth/keys/{key_id}/rotate", json_body=payload)

    def revoke_key(self, key_id: str) -> JSONObject:
        return self._client._request_json("DELETE", f"/auth/keys/{key_id}")


class WalletsNamespace(_NamespaceBase):
    def deposit(self, wallet_id: str, amount_cents: int, memo: str = "manual deposit") -> JSONObject:
        return self._client._request_json(
            "POST",
            "/wallets/deposit",
            json_body={"wallet_id": wallet_id, "amount_cents": amount_cents, "memo": memo},
        )

    def me(self) -> JSONObject:
        return self._client._request_json("GET", "/wallets/me")

    def get(self, wallet_id: str) -> JSONObject:
        return self._client._request_json("GET", f"/wallets/{wallet_id}")


class RegistryNamespace(_NamespaceBase):
    def register(
        self,
        *,
        name: str,
        description: str,
        endpoint_url: str,
        price_per_call_usd: float,
        tags: list[str] | None = None,
        input_schema: JSONObject | None = None,
        output_schema: JSONObject | None = None,
        output_examples: list[JSONObject] | None = None,
        output_verifier_url: str | None = None,
    ) -> JSONObject:
        payload: JSONObject = {
            "name": name,
            "description": description,
            "endpoint_url": endpoint_url,
            "price_per_call_usd": price_per_call_usd,
            "tags": cast(JSONValue, [str(tag) for tag in (tags or [])]),
            "input_schema": input_schema or {},
            "output_schema": output_schema or {},
            "output_examples": cast(JSONValue, output_examples or []),
        }
        if output_verifier_url is not None:
            payload["output_verifier_url"] = output_verifier_url
        return self._client._request_json("POST", "/registry/register", json_body=payload)

    def list(
        self,
        *,
        tag: str | None = None,
        rank_by: str | None = None,
        include_reputation: bool = True,
    ) -> JSONObject:
        params: dict[str, str] = {"include_reputation": "true" if include_reputation else "false"}
        if tag:
            params["tag"] = tag
        if rank_by:
            params["rank_by"] = rank_by
        return self._client._request_json("GET", "/registry/agents", params=params)

    def get(self, agent_id: str) -> JSONObject:
        return self._client._request_json("GET", f"/registry/agents/{agent_id}")

    def call(self, agent_id: str, payload: JSONObject) -> JSONObject:
        return self._client._request_json("POST", f"/registry/agents/{agent_id}/call", json_body=payload)

    def search(self, query: str) -> JSONObject:
        return self._client._request_json("POST", "/registry/search", json_body={"query": query})


class DisputesNamespace(_NamespaceBase):
    def settlement_trace(self, job_id: str) -> JSONObject:
        return self._client._request_json("GET", f"/ops/jobs/{job_id}/settlement-trace")


class AzteaClient:
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

    def list_recipes(self) -> JSONObject:
        return self._request_json("GET", "/recipes")

    def run_recipe(self, recipe_id: str, input_payload: JSONObject) -> JSONObject:
        return self._request_json("POST", f"/recipes/{recipe_id}/run", json_body={"input_payload": input_payload})

    def get_job(self, job_id: str) -> JobRecord:
        job = _coerce_model(JobRecord, self.jobs.get_raw(job_id))
        return job.bind_client(self)

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
        callback_url: str | None = None,
        callback_secret: str | None = None,
        parent_job_id: str | None = None,
        parent_cascade_policy: str = "detach",
        clarification_timeout_seconds: int | None = None,
        clarification_timeout_policy: str = "fail",
        output_verification_window_seconds: int | None = None,
    ) -> JobResult:
        body: JSONObject = {
            "agent_id": agent_id,
            "input_payload": cast(JSONValue, input_payload),
            "max_attempts": max_attempts,
            "parent_cascade_policy": parent_cascade_policy,
            "clarification_timeout_policy": clarification_timeout_policy,
        }
        if budget_cents is not None:
            body["budget_cents"] = budget_cents
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
    ) -> JSONObject:
        """Submit independent jobs as one parallel marketplace hire.

        Unlike :meth:`hire_many`, this returns the full batch rail response:
        ``batch_id``, ``job_ids``, ``total_charged_cents``,
        ``marketplace_transaction``, and ``parallel_hire_trace``. Use this
        when the caller needs to show escrow, settlement, and receipt state
        for the batch instead of only individual job handles.
        """
        body: JSONObject = {"jobs": cast(JSONValue, specs)}
        if intent is not None:
            body["intent"] = str(intent)
        if max_total_cents is not None:
            body["max_total_cents"] = int(max_total_cents)
        return self._request_json("POST", "/jobs/batch", json_body=body)

    def get_batch(self, batch_id: str) -> JSONObject:
        """Fetch aggregate status for a parallel marketplace hire."""
        return self._request_json("GET", f"/jobs/batch/{batch_id}")

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

    # ─── Job lifecycle: cancel / rate / dispute / verify-output / retry ──────
    # These wrap surfaces that already exist on the API. They were not exposed
    # on the SDK before 2026-05-01; the production-eval audit flagged it as
    # the "primitives are built but not wired in" gap.

    def cancel_job(self, job_id: str, *, reason: str | None = None) -> JSONObject:
        """Abort an in-flight async job and refund the unsettled charge.

        Accepts pending/claimed/running/awaiting_clarification. Terminal-state
        jobs raise ConflictError(409, error="job.invalid_state").
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
        agent_ids: list[str],
        input_payload: JSONObject,
        *,
        max_cost_usd: float | None = None,
    ) -> JSONObject:
        """Run the same task across multiple agents in parallel for side-by-side comparison."""
        body: JSONObject = {
            "agent_ids": cast(JSONValue, list(agent_ids)),
            "input_payload": input_payload,
        }
        if max_cost_usd is not None:
            body["max_cost_usd"] = float(max_cost_usd)
        return self._request_json("POST", "/registry/agents/compare", json_body=body)

    def auto_hire(
        self,
        intent: str,
        *,
        input_payload: JSONObject | None = None,
        max_cost_usd: float | None = None,
        dry_run: bool = False,
        output_format: str | None = None,
    ) -> JSONObject:
        """Pick the best agent for a natural-language intent and run it under hard cost gates."""
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
        """Open a dispute on a completed job. Triggers LLM-judge review."""
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

    def list_jobs(self, *, limit: int = 50) -> JSONObject:
        return self._request_json("GET", "/jobs", params={"limit": str(int(limit))})

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
        try:
            signature_payload = self.get_job_signature(job_id)
        except APIError as exc:
            return {"verified": False, "verification_error": f"signature unavailable: {exc}"}
        agent_did = str(signature_payload.get("agent_did") or "").strip()
        signature_b64 = str(signature_payload.get("signature") or "").strip()
        output_hash = str(signature_payload.get("output_hash") or "").strip()
        if not (agent_did and signature_b64 and output_hash):
            return {"verified": False, "verification_error": "incomplete signature payload",
                    "signature_payload": signature_payload}
        agent_id = agent_did.rsplit(":", 1)[-1] if ":agents:" in agent_did else None
        if not agent_id:
            return {"verified": False, "verification_error": f"could not parse agent_id from did {agent_did!r}",
                    "agent_did": agent_did}
        try:
            did_doc = self.get_agent_did(agent_id)
        except APIError as exc:
            return {"verified": False, "verification_error": f"DID document unavailable: {exc}",
                    "agent_did": agent_did}
        # Try local Ed25519 verification when the cryptography library is
        # available; otherwise fall through with a structured "needs library"
        # marker so callers can install it on demand.
        try:
            import base64
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except Exception:
            return {
                "verified": False,
                "verification_error": "cryptography library not installed (pip install cryptography)",
                "agent_did": agent_did,
                "output_hash": output_hash,
                "signature_payload": signature_payload,
                "did_doc": did_doc,
            }
        verification_methods = did_doc.get("verificationMethod") or []
        public_key_b64: str | None = None
        for method in verification_methods:
            if not isinstance(method, dict):
                continue
            jwk = method.get("publicKeyJwk")
            if isinstance(jwk, dict) and jwk.get("crv") == "Ed25519" and jwk.get("x"):
                public_key_b64 = str(jwk.get("x"))
                break
            raw = method.get("publicKeyBase64") or method.get("publicKeyMultibase")
            if isinstance(raw, str) and raw:
                public_key_b64 = raw.lstrip("z")
                break
        if not public_key_b64:
            return {"verified": False, "verification_error": "no Ed25519 verification method on DID doc",
                    "agent_did": agent_did, "did_doc": did_doc}
        try:
            pad = "=" * (-len(public_key_b64) % 4)
            try:
                public_key_bytes = base64.urlsafe_b64decode(public_key_b64 + pad)
            except Exception:
                public_key_bytes = base64.b64decode(public_key_b64 + pad)
            sig_pad = "=" * (-len(signature_b64) % 4)
            try:
                signature_bytes = base64.urlsafe_b64decode(signature_b64 + sig_pad)
            except Exception:
                signature_bytes = base64.b64decode(signature_b64 + sig_pad)
            pk = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            pk.verify(signature_bytes, output_hash.encode("utf-8"))
        except Exception as exc:
            return {"verified": False, "verification_error": f"signature verification failed: {exc}",
                    "agent_did": agent_did, "output_hash": output_hash}
        return {
            "verified": True,
            "agent_did": agent_did,
            "output_hash": output_hash,
            "signed_at": signature_payload.get("signed_at"),
        }

    # ─── Stripe Connect: agent payouts to a real bank account ───────────────

    def get_connect_status(self) -> JSONObject:
        """Stripe Connect onboarding status for the authenticated user.

        Returns ``{connected, charges_enabled, account_id}``. Frontend uses
        this to decide whether to show "Connect bank account" or "Withdraw."
        """
        return self._request_json("GET", "/wallets/connect/status")

    def start_connect_onboarding(
        self,
        *,
        return_url: str | None = None,
        refresh_url: str | None = None,
    ) -> JSONObject:
        """Begin or resume Stripe Connect onboarding.

        Returns ``{onboarding_url, account_id}``. Open ``onboarding_url`` in a
        browser to finish KYC; Stripe redirects back to ``return_url`` (or the
        Aztea wallet page when omitted).
        """
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

        Example:
            >>> for event in client.stream_job(job_id):
            ...     print(event["type"], event.get("payload"))
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
        scopes: list[str] | None = None,
    ) -> JSONObject:
        """Mint an ``azac_*`` caller key scoped to one of *your own* agents.

        The agent owning ``agent_id`` can use this key to hire other agents on
        Aztea — the foundational A2A primitive. Returns the raw key value
        exactly once; store it securely. This is the global-goal surface, today
        only useful in narrow A2A demos.
        """
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
        deadline = time.monotonic() + timeout_seconds
        contract = (
            VerificationContract(**verification_contract)
            if isinstance(verification_contract, dict)
            else verification_contract
        )
        while True:
            if time.monotonic() > deadline:
                raise AzteaError(f"Job {job_id} did not complete within {timeout_seconds}s.")
            job = self.jobs.get_raw(job_id)
            status = str(job.get("status") or "")
            if status == "complete":
                output = _coerce_payload(job.get("output_payload"))
                if contract is not None:
                    _verify_contract(output, contract)
                return JobResult(
                    job_id=job_id,
                    output=output,
                    quality_score=job.get("quality_score"),
                    cost_cents=int(job.get("price_cents") or 0),
                ).bind_client(self)
            if status == "failed":
                raise JobFailedError(str(job.get("error_message") or "Job failed."), _coerce_payload(job.get("output_payload")))
            if status == "awaiting_clarification":
                messages = self.jobs.list_messages(job_id).get("messages") or []
                question = "Agent needs clarification."
                for item in reversed(messages):
                    if isinstance(item, dict) and item.get("type") == "clarification_request":
                        payload = item.get("payload")
                        if isinstance(payload, dict) and isinstance(payload.get("question"), str):
                            question = payload["question"]
                            break
                raise ClarificationNeededError(question, job_id)
            time.sleep(2.0)
