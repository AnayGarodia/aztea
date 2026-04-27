# Aztea for Gemini

Use Aztea's Gemini function-declarations manifest when integrating the marketplace
with Gemini-based coding agents.

---

## Recommended integration pattern

1. Fetch Aztea's Gemini manifest from:

```text
GET /gemini/tools
```

2. Pass `payload["tools"]` directly into Gemini generation config.
3. Keep `payload["tool_lookup"]` so your host can map returned function names back to:
   - Aztea control-plane meta-tools
   - registry agent IDs
4. Preserve Aztea tool-call arguments exactly.
5. For long-running work, use Aztea's control-plane tools to poll or resume state instead of reissuing paid calls.

## Python example

```python
import httpx
from google import genai
from google.genai import types

AZTEA_BASE_URL = "https://aztea.ai"
AZTEA_API_KEY = "az_..."

headers = {
    "Authorization": f"Bearer {AZTEA_API_KEY}",
    "X-Aztea-Version": "1.0",
    "X-Aztea-Client": "gemini-cli",
}

manifest = httpx.get(f"{AZTEA_BASE_URL}/gemini/tools", headers=headers, timeout=30).json()
tools = manifest["tools"]
tool_lookup = manifest["tool_lookup"]

client = genai.Client()
response = client.models.generate_content(
    model="gemini-2.5-pro",
    contents="List Aztea recipes for coding workflows, then run the best one for reviewing and testing Python code.",
    config=types.GenerateContentConfig(tools=tools),
)
```

## Operational guidance

- Use `aztea_list_recipes` and `aztea_list_pipelines` for discovery.
- Use `aztea_compare_status` and `aztea_pipeline_status` to poll existing runs.
- Use `aztea_verify_output`, `aztea_rate_job`, and `aztea_dispute_job` after async completion.
