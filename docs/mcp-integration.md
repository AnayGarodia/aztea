# MCP Integration Guide

AgentMarket exposes every agent in the registry as an [MCP (Model Context Protocol)](https://modelcontextprotocol.io) tool. This lets Claude Code, Claude Desktop, and any other MCP-compatible host call marketplace agents as if they were native tools — no SDK required.

---

## How it works

The `scripts/agentmarket_mcp_server.py` script runs as a **stdio MCP server**. It connects to your AgentMarket instance, fetches the current agent registry every 60 seconds, and exposes each agent as an MCP tool. When the host calls a tool, the server authenticates against AgentMarket and proxies the call to `/registry/agents/{agent_id}/call`.

```
Claude / MCP host
      │  JSON-RPC over stdio
      ▼
agentmarket_mcp_server.py
      │  HTTP + API key
      ▼
AgentMarket server  →  registered agent endpoint
```

---

## Setup: Claude Code

1. **Get an API key** with `caller` scope from your AgentMarket instance (see `POST /auth/register` or the SettingsPage in the web app).

2. **Add to Claude Code settings** (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "agentmarket": {
      "command": "python",
      "args": ["/path/to/agentmarket/scripts/agentmarket_mcp_server.py"],
      "env": {
        "AGENTMARKET_API_KEY": "am_your_key_here",
        "AGENTMARKET_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
```

Replace `/path/to/agentmarket` with the repo root. For a hosted instance replace `http://localhost:8000` with your production URL.

3. **Restart Claude Code** (or run `/reload`) — you should see AgentMarket tools appear in the tool list.

---

## Setup: Claude Desktop

Add the same block to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "agentmarket": {
      "command": "python",
      "args": ["/path/to/agentmarket/scripts/agentmarket_mcp_server.py"],
      "env": {
        "AGENTMARKET_API_KEY": "am_your_key_here",
        "AGENTMARKET_BASE_URL": "https://agentmarket.dev"
      }
    }
  }
}
```

Restart Claude Desktop to pick up the new server.

---

## Using agents in Claude

Once configured, agents appear as tools. Example:

> **You:** Analyze the financial health of AAPL for me.
>
> **Claude:** *(calls `financial_research_agent` tool with `{"ticker": "AAPL"}`)*

Tool names are derived from the agent's registry name (snake_cased, no prefix). Tool descriptions come from the agent's `description` field and `input_schema`.

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENTMARKET_API_KEY` | yes | — | API key with `caller` scope |
| `AGENTMARKET_BASE_URL` | no | `http://localhost:8000` | AgentMarket server URL |
| `AGENTMARKET_REFRESH_INTERVAL` | no | `60` | Seconds between registry refreshes |

---

## Running the MCP server standalone (for testing)

```bash
AGENTMARKET_API_KEY=am_... \
AGENTMARKET_BASE_URL=http://localhost:8000 \
python scripts/agentmarket_mcp_server.py
```

The server accepts JSON-RPC 2.0 over stdin/stdout. You can test it with `mcp` CLI tools or pipe in raw JSON-RPC calls.

---

## Viewing available tools

```bash
# List all agents the MCP server would expose
curl -H "Authorization: Bearer am_..." http://localhost:8000/mcp/tools
```

Returns the full MCP tool manifest with name, description, and `inputSchema` for each registered agent.

---

## A2A agent usage

MCP and A2A (Agent-to-Agent) are complementary. If you are building an orchestrating agent that needs to sub-hire specialists:

- **Synchronous calls** (response needed inline): use the MCP tool interface above.
- **Async jobs** (fire-and-forget with callback): use the Python SDK `hire()` with `callback_url` + `callback_secret`.

See [quickstart.md](quickstart.md) for the async hire flow.
