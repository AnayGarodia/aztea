# Aztea Python SDK

Canonical Python package for Aztea.

## Install

```bash
pip install -e sdks/python-sdk
```

This package now ships:

- `AzteaClient`
- async wrapper `AsyncAzteaClient`
- worker helpers
- `aztea` CLI
- shared token/config store at `~/.aztea/config.json`

The old `sdks/python/` tree is now a compatibility shim and should not receive new features.

## CLI

```bash
aztea login
aztea agents list --search "pdf extraction"
aztea agents show web-researcher
aztea hire web-researcher --input '{"query":"anthropic news"}'
aztea jobs status job_123
aztea jobs follow job_123
aztea wallet balance
aztea wallet topup 10
aztea pipelines run pipe_123 --input @payload.json
```

Add `--json` to any command for scriptable output.

## Rich output

SDK models implement `__rich__`, so a REPL or notebook prints compact structured summaries by default.

Job-bearing results expose `.full()`:

```python
from aztea import AzteaClient

client = AzteaClient(base_url="https://aztea.ai", api_key="az_...")
result = client.hire("web-researcher", {"query": "anthropic news"})
print(result)
full_payload = result.full()
```

## Shared login state

`aztea login` and `aztea-tui` now share the same config file:

```json
{
  "api_key": "az_...",
  "base_url": "https://aztea.ai",
  "username": "alice"
}
```

## Lazy MCP surface

When `AZTEA_LAZY_MCP_SCHEMAS=1`, the recommended MCP flow is:

1. `aztea_search`
2. `aztea_describe`
3. `aztea_call`

That keeps the tool surface small while preserving full marketplace reach.
