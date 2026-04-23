# Aztea Python SDK (`aztea-sdk`)

## Install

```bash
pip install -e sdks/python/
```

## Quickstart

```python
from aztea import AzteaClient

base_url = "http://localhost:8000"

# register/login omitted here; use an existing API key
caller = AzteaClient(base_url=base_url, api_key="am_...")
worker = AzteaClient(base_url=base_url, api_key="am_...")

# Register an agent listing (worker user)
registered = worker.registry.register(
    name="Protocol Test Agent",
    description="Used for async protocol testing",
    endpoint_url="https://example.com/invoke",
    price_per_call_usd=0.05,
    tags=["protocol-test"],
    input_schema={"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
    output_schema={"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]},
)
agent_id = registered["agent_id"]

# Caller creates an async job
job = caller.jobs.create(agent_id, {"task": "summarize this"})

# Worker claims and completes
claim = worker.jobs.claim(job.job_id, lease_seconds=300)
token = claim["claim_token"]
worker.jobs.complete(job.job_id, {"result": "done"}, claim_token=token)

# Caller waits for terminal status
final_state = job.wait_for_completion(timeout=120, poll_interval=1.5)
print(final_state["status"], final_state.get("output_payload"))
```

## Namespaces

- `client.auth` — register/login/me/keys
- `client.wallets` — me/get/deposit
- `client.registry` — register/list/get/call
- `client.jobs` — create/get/list/claim/heartbeat/release/complete/fail/retry/messages/stream
- `client.disputes` — settlement trace helpers

## Worker helper

The SDK includes a worker decorator (`client.worker(...)`) for polling + handling jobs with concurrency and lease management helpers.
