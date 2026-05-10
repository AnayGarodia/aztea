"""Shallow coverage for AsyncAzteaClient.

The async client is a thin wrapper that defers every method to its sync
sibling via ``asyncio.to_thread``. The risk a future change introduces is
*delegation drift*: an async method that no longer mirrors its sync
counterpart's behavior (renamed kwarg, dropped return value, swallowed
exception).

These tests don't exercise the underlying HTTP — they monkey-patch each
sync method on the embedded ``AzteaClient`` to a recorder and assert
that the async wrapper:
  * forwards all positional + keyword args verbatim,
  * returns the sync method's return value, and
  * surfaces exceptions raised by the sync method.

A single parametrised test covers the 27 ``*args, **kwargs`` wrappers;
the no-arg methods (``close``, ``get_balance``, ``get_wallet``,
``list_hooks``) get dedicated tests because their wrapper signatures
differ.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aztea.async_client import AsyncAzteaClient


_PASSTHROUGH_METHODS = [
    "search_agents",
    "list_agents",
    "get_agent",
    "deposit",
    "get_spend_summary",
    "create_topup_session",
    "get_job",
    "get_job_full_output",
    "hire",
    "wait_for",
    "hire_many",
    "list_pipelines",
    "get_pipeline",
    "run_pipeline",
    "get_pipeline_run",
    "list_recipes",
    "run_recipe",
    "decide_output_verification",
    "clarify",
    "hire_with_clarification",
    "hire_async",
    "register_hook",
    "delete_hook",
]

_NOARG_METHODS = ["get_balance", "get_wallet", "list_hooks"]


def _make_recorder(return_value: Any):
    captured: dict[str, Any] = {"args": None, "kwargs": None, "calls": 0}

    def _stub(*args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["calls"] += 1
        return return_value

    return _stub, captured


@pytest.mark.parametrize("method_name", _PASSTHROUGH_METHODS)
def test_async_method_delegates_to_sync_counterpart(method_name: str) -> None:
    client = AsyncAzteaClient(base_url="http://test.local", api_key="k")
    return_value = {"_method": method_name, "ok": True}
    stub, captured = _make_recorder(return_value)
    setattr(client._client, method_name, stub)

    args = ("positional-arg",)
    kwargs = {"kw_arg": 42, "other": [1, 2]}
    result = asyncio.run(getattr(client, method_name)(*args, **kwargs))

    assert result == return_value, f"{method_name} dropped the return value"
    assert captured["calls"] == 1
    assert captured["args"] == args
    assert captured["kwargs"] == kwargs


@pytest.mark.parametrize("method_name", _NOARG_METHODS)
def test_async_noarg_method_delegates_to_sync(method_name: str) -> None:
    client = AsyncAzteaClient(base_url="http://test.local", api_key="k")
    stub, captured = _make_recorder({"_method": method_name})
    setattr(client._client, method_name, stub)

    result = asyncio.run(getattr(client, method_name)())

    assert result == {"_method": method_name}
    assert captured["calls"] == 1
    assert captured["args"] == ()
    assert captured["kwargs"] == {}


def test_async_method_propagates_sync_exception() -> None:
    client = AsyncAzteaClient(base_url="http://test.local", api_key="k")

    def _raiser(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("sync side blew up")

    client._client.hire = _raiser  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="sync side blew up"):
        asyncio.run(client.hire(agent_id="x", input_payload={}))


def test_async_close_delegates_to_sync_close() -> None:
    client = AsyncAzteaClient(base_url="http://test.local", api_key="k")
    stub, captured = _make_recorder(None)
    client._client.close = stub  # type: ignore[method-assign]

    asyncio.run(client.close())

    assert captured["calls"] == 1


def test_async_aenter_aexit_works_as_context_manager() -> None:
    async def _main() -> AsyncAzteaClient:
        client = AsyncAzteaClient(base_url="http://test.local", api_key="k")
        captured = {"closed": 0}

        def _close_stub() -> None:
            captured["closed"] += 1

        client._client.close = _close_stub  # type: ignore[method-assign]
        async with client as c:
            assert c is client
        assert captured["closed"] == 1
        return client

    asyncio.run(_main())


def test_async_method_set_matches_documented_surface() -> None:
    """Drift guard: every coroutine method on AsyncAzteaClient must be
    accounted for by this test file. A new sync method getting an async
    wrapper but no test would slip past delegation coverage; this assertion
    lists it explicitly."""
    actual_async_methods = {
        name
        for name in dir(AsyncAzteaClient)
        if not name.startswith("_")
        and asyncio.iscoroutinefunction(getattr(AsyncAzteaClient, name))
    }
    documented = set(_PASSTHROUGH_METHODS) | set(_NOARG_METHODS) | {"close"}
    untested = actual_async_methods - documented
    assert not untested, (
        f"Async methods on AsyncAzteaClient with no test coverage: {sorted(untested)}. "
        "Add them to _PASSTHROUGH_METHODS or _NOARG_METHODS in this file."
    )
