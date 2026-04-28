from __future__ import annotations

import asyncio
from typing import Any

from .client import AzteaClient


class AsyncAzteaClient:
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        client_id: str = "aztea-python-sdk-async",
        timeout: float = 30.0,
    ) -> None:
        self._client = AzteaClient(
            base_url=base_url,
            api_key=api_key,
            client_id=client_id,
            timeout=timeout,
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)

    async def __aenter__(self) -> "AsyncAzteaClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    def set_api_key(self, api_key: str | None) -> None:
        self._client.set_api_key(api_key)

    async def search_agents(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.search_agents, *args, **kwargs)

    async def list_agents(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.list_agents, *args, **kwargs)

    async def get_agent(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.get_agent, *args, **kwargs)

    async def get_balance(self) -> int:
        return await asyncio.to_thread(self._client.get_balance)

    async def get_wallet(self) -> Any:
        return await asyncio.to_thread(self._client.get_wallet)

    async def deposit(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.deposit, *args, **kwargs)

    async def get_spend_summary(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.get_spend_summary, *args, **kwargs)

    async def create_topup_session(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.create_topup_session, *args, **kwargs)

    async def get_job(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.get_job, *args, **kwargs)

    async def get_job_full_output(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.get_job_full_output, *args, **kwargs)

    async def hire(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.hire, *args, **kwargs)

    async def wait_for(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.wait_for, *args, **kwargs)

    async def hire_many(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.hire_many, *args, **kwargs)

    async def list_pipelines(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.list_pipelines, *args, **kwargs)

    async def get_pipeline(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.get_pipeline, *args, **kwargs)

    async def run_pipeline(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.run_pipeline, *args, **kwargs)

    async def get_pipeline_run(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.get_pipeline_run, *args, **kwargs)

    async def list_recipes(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.list_recipes, *args, **kwargs)

    async def run_recipe(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.run_recipe, *args, **kwargs)

    async def decide_output_verification(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.decide_output_verification, *args, **kwargs)

    async def clarify(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.clarify, *args, **kwargs)

    async def hire_with_clarification(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.hire_with_clarification, *args, **kwargs)

    async def hire_async(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.hire_async, *args, **kwargs)

    async def register_hook(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.register_hook, *args, **kwargs)

    async def list_hooks(self) -> Any:
        return await asyncio.to_thread(self._client.list_hooks)

    async def delete_hook(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._client.delete_hook, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
