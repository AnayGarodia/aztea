from __future__ import annotations

from typing import Iterable, cast

from ..types import JSONObject, JSONValue
from ._helpers import _NamespaceBase


class AuthNamespace(_NamespaceBase):
    def register(self, username: str, email: str, password: str) -> JSONObject:
        return self._client._request_json(
            "POST",
            "/auth/register",
            json_body={"username": username, "email": email, "password": password},
            require_api_key=False,
        )

    def login(
        self,
        email: str,
        password: str,
        *,
        rotate: bool = False,
        username: str | None = None,
    ) -> JSONObject:
        body: JSONObject = {"email": email, "password": password, "rotate": rotate}
        if username is not None:
            body["username"] = username
        return self._client._request_json(
            "POST",
            "/auth/login",
            json_body=body,
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
