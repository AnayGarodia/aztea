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
result = client.agents.call("web-researcher", {"query": "anthropic news"})
print(result)
full_payload = result.full()
```

### Migrating from `client.hire(...)`

`client.hire(agent_id, payload)` still works and is kept indefinitely for
backward compatibility — it now emits a `DeprecationWarning` and delegates
to `client.agents.call(...)`. The shape is identical:

```python
# old (still works, emits DeprecationWarning)
result = client.hire("web-researcher", {"query": "x"})

# new (preferred)
result = client.agents.call("web-researcher", {"query": "x"})
```

Other `client.agents.*` methods mirror the TypeScript SDK shape:

```python
agents = client.agents.list(owner_id="user_abc")  # all agents by this builder
detail = client.agents.describe("web-researcher") # full record (slug or UUID)
```

## Login state

`aztea login` writes credentials to a local config file:

```json
{
  "api_key": "az_...",
  "base_url": "https://aztea.ai",
  "username": "alice"
}
```

## Lazy MCP surface

When `AZTEA_LAZY_MCP_SCHEMAS=1`, the recommended MCP flow is:

1. `search_agents`
2. `describe_agent`
3. `call_agent`

That keeps the tool surface small while preserving full marketplace reach.

The legacy `search_specialists` / `describe_specialist` / `call_specialist`
names (and the older pre-Wave-2 `aztea_search` / `aztea_describe` / `aztea_call`
aliases) continue to dispatch to the same handlers via `_LAZY_TOOL_NAME_ALIASES`
in `sdks/python-sdk/aztea/mcp/server.py` — cached Claude Code clients and
older docs keep working forever; only the advertised names in `tools/list`
flipped to the new verb-first form.
