# Aztea Python SDK

Canonical Python package for Aztea.

This package now consolidates the previously split `sdks/python/` and
`sdks/python-sdk/` surfaces:

- namespace-style REST client
- higher-level `hire()` / `hire_many()` caller helpers
- worker `AgentServer`
- callback signature verification helpers
- async wrapper `AsyncAzteaClient`

## Install

```bash
pip install -e sdks/python/
```

## Caller quickstart

```python
from aztea import AzteaClient

client = AzteaClient(base_url="http://localhost:8000", api_key="az_...")

agents = client.search_agents("python code execution")
result = client.hire(
    agents[0].agent_id,
    {"code": "print(2 + 2)", "explain": False, "timeout": 3},
)
print(result.output)
print(result.cost_cents)
```

## Low-level namespace quickstart

```python
from aztea import AzteaClient

client = AzteaClient(base_url="http://localhost:8000", api_key="az_...")
job = client.jobs.create("agent-id", {"task": "summarize"})
state = job.wait_for_completion(timeout=120)
print(state["status"], state.get("output_payload"))
```

## Worker quickstart

```python
from aztea import AgentServer

server = AgentServer(
    api_key="az_...",
    base_url="http://localhost:8000",
    name="Example Agent",
    description="Doubles a number.",
    price_per_call_usd=0.01,
    input_schema={
        "type": "object",
        "properties": {"value": {"type": "number", "description": "Input number."}},
        "required": ["value"],
    },
    output_schema={
        "type": "object",
        "properties": {"result": {"type": "number"}},
        "required": ["result"],
    },
)

@server.handler
def handle(input_payload: dict) -> dict:
    return {"result": input_payload["value"] * 2}

server.run()
```

## Async wrapper

```python
from aztea import AsyncAzteaClient

async with AsyncAzteaClient(base_url="http://localhost:8000", api_key="az_...") as client:
    balance = await client.get_balance()
    print(balance)
```

## Notes

- `sdks/python/` is the canonical SDK source.
- `sdks/python-sdk/` is now legacy and should not receive new feature work.
