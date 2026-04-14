from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, cast

import requests

from .errors import AgentmarketError, raise_for_error_response
from .jobs import JobsNamespace
from .types import JSONObject, JSONValue
from .workers import JobSource, build_worker_decorator


def _ensure_object(value: Any, *, context: str) -> JSONObject:
    if isinstance(value, dict):
        return value
    raise AgentmarketError(f"{context} expected a JSON object response, got: {type(value).__name__}.")


@dataclass
class _NamespaceBase:
    _client: "AgentmarketClient"


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
    ) -> JSONObject:
        payload: JSONObject = {
            "name": name,
            "description": description,
            "endpoint_url": endpoint_url,
            "price_per_call_usd": price_per_call_usd,
            "tags": cast(JSONValue, [str(tag) for tag in (tags or [])]),
            "input_schema": input_schema or {},
        }
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


class DisputesNamespace(_NamespaceBase):
    def settlement_trace(self, job_id: str) -> JSONObject:
        return self._client._request_json("GET", f"/ops/jobs/{job_id}/settlement-trace")


class AgentmarketClient:
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._api_key = api_key
        self._session = requests.Session()

        self.auth = AuthNamespace(self)
        self.wallets = WalletsNamespace(self)
        self.registry = RegistryNamespace(self)
        self.jobs = JobsNamespace(self)
        self.disputes = DisputesNamespace(self)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "AgentmarketClient":
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
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif require_api_key:
            raise AgentmarketError("This operation requires an API key.")
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
