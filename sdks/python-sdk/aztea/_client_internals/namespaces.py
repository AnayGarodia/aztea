from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, cast

from ..types import JSONObject, JSONValue
from ._helpers import _NamespaceBase

if TYPE_CHECKING:
    from ..models import Agent, JobResult, VerificationContract


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
    def __call__(self) -> "DisputesNamespace":
        return self

    def settlement_trace(self, job_id: str) -> JSONObject:
        return self._client._request_json("GET", f"/ops/jobs/{job_id}/settlement-trace")


class AgentsNamespace(_NamespaceBase):
    """High-level agent operations mirroring the TypeScript SDK's `client.agents.*`.

    This namespace is the Wave 2 (2026-05-26) preferred surface for hiring and
    inspecting agents. The shape mirrors `@aztea/sdk` (TypeScript) so a polyglot
    team can context-switch without re-learning the API:

      client.agents.call(name_or_id, payload)  # hire and wait for result
      client.agents.list(owner_id=...)         # browse the catalog
      client.agents.describe(name_or_id)       # full agent record

    The legacy `client.hire()` method delegates here and emits a
    DeprecationWarning. Both are stable; only the framing changes.

    Reference resolution: `name_or_id` accepts a UUID, a slug (snake_case or
    kebab-case), or the human-readable display name — see
    AzteaClient._resolve_agent_reference for the lookup order.
    """

    def call(
        self,
        name_or_id: str,
        payload: JSONObject,
        *,
        verification_contract: "VerificationContract | dict[str, Any] | None" = None,
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
    ) -> "JobResult":
        """Hire an agent and (by default) wait for its terminal result.

        Identical contract to `client.hire()` — same kwargs, same return
        type, same exceptions — but the method name aligns with the platform
        identity (`agents.call`) rather than the legacy marketplace framing
        (`hire`). See the AzteaClient class-level docstring for the full
        exception hierarchy.
        """
        # Delegate to the private impl, NOT to client.hire() — calling hire()
        # would re-trigger its DeprecationWarning on every legitimate
        # agents.call() invocation.
        return self._client._call_agent_impl(  # type: ignore[no-any-return]
            name_or_id,
            payload,
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

    def list(
        self,
        *,
        owner_id: str | None = None,
        tag: str | None = None,
        rank_by: str = "trust",
        include_reputation: bool = True,
    ) -> list["Agent"]:
        """List catalog agents, optionally filtered by owner / tag.

        `owner_id` lets a caller browse all agents published by a single
        builder — the buyer-facing version of "what has this builder shipped"
        used by the Wave 2 builder profile pages. The backend filter on
        `/registry/agents?owner_id=...` lands in the same Wave 2 batch.
        """
        from ..models import Agent  # local import — avoid circular at module load
        from ._helpers import _coerce_model

        params: dict[str, str] = {
            "include_reputation": "true" if include_reputation else "false",
        }
        if tag:
            params["tag"] = tag
        if rank_by:
            params["rank_by"] = rank_by
        if owner_id:
            params["owner_id"] = owner_id
        data = self._client._request_json("GET", "/registry/agents", params=params)
        raw_agents = data.get("agents") or []
        return [_coerce_model(Agent, item) for item in raw_agents if isinstance(item, dict)]

    def describe(self, name_or_id: str) -> "Agent":
        """Resolve `name_or_id` to a UUID and fetch the full agent record."""
        resolved = self._client._resolve_agent_reference(name_or_id)
        return self._client.get_agent(resolved)
