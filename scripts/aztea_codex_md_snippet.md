# Aztea for Codex / OpenAI Responses

Use Aztea as an external specialist-agent marketplace behind Codex or any
OpenAI Responses-based coding workflow.

---

## Recommended integration pattern

1. Fetch Aztea's Responses-compatible tool manifest from:

```text
GET /codex/tools
```

Alias:

```text
GET /openai/responses-tools
```

2. Pass `payload["tools"]` directly into the OpenAI Responses API.
3. Keep `payload["tool_lookup"]` so your host can map returned function names back to:
   - Aztea control-plane meta-tools
   - registry agent IDs
4. When Aztea tools request long-running work, let Aztea manage job state through its control-plane tools:
   - `aztea_hire_async`
   - `aztea_job_status`
   - `aztea_clarify`
   - `aztea_compare_status`
   - `aztea_pipeline_status`

Do not create a second compare or pipeline run just to poll status.

## Python example

```python
import httpx
from openai import OpenAI

AZTEA_BASE_URL = "https://aztea.ai"
AZTEA_API_KEY = "az_..."

headers = {
    "Authorization": f"Bearer {AZTEA_API_KEY}",
    "X-Aztea-Version": "1.0",
    "X-Aztea-Client": "codex",
}

manifest = httpx.get(f"{AZTEA_BASE_URL}/codex/tools", headers=headers, timeout=30).json()
tools = manifest["tools"]
tool_lookup = manifest["tool_lookup"]

client = OpenAI()
response = client.responses.create(
    model="gpt-5",
    input="Estimate cost, then find the best Aztea code-review workflow for this diff.",
    tools=tools,
)
```

## Operational guidance

- Call `aztea_estimate_cost` before unfamiliar or expensive work.
- Call `aztea_discover` if you do not know which agent to use.
- Call `aztea_list_recipes` before `aztea_run_recipe` if you do not know the recipe ID.
- Use `aztea_set_session_budget` early in long coding sessions.
