# MCP Integration

Aztea plugs into Claude Code (and Claude Desktop) as an MCP server. One install gives Claude access to the full tool catalog — code review, test generation, PR review, dependency auditing, live web research, CVE lookups, arXiv papers, and more.

---

## Quickstart: one command

```bash
npx aztea-cli init
```

This creates a free account (or logs you into an existing one), adds $2 of free credit, and writes the MCP config to `~/.claude/settings.json` automatically. Restart Claude Code and the full catalog appears.

**Requires:** Node.js 18+ and [Claude Code](https://claude.ai/code).

---

## Manual setup

If you prefer to configure by hand, add this to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "aztea": {
      "command": "npx",
      "args": ["-y", "aztea-cli", "mcp"],
      "env": {
        "AZTEA_API_KEY": "your-key-here",
        "AZTEA_BASE_URL": "https://aztea.ai"
      }
    }
  }
}
```

Get your API key from [aztea.ai/keys](https://aztea.ai/keys) after signing up.

---

## Claude Desktop

Same config, different file. Add the `mcpServers` block to:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

---

## What's in the catalog

| Tool | What it does | Category |
|------|-------------|----------|
| PR Reviewer | Reviews a GitHub PR or raw diff, returns findings by severity | Code |
| Test Generator | Source code → runnable test suite (pytest, Jest, Vitest, JUnit…) | Code |
| Code Reviewer | Structured code review with CWE IDs and copy-paste fixes | Code |
| Spec Writer | Requirements → PRD, RFC, ADR, or API spec | Code |
| Dependency Auditor | package.json / requirements.txt → CVEs + upgrade recommendations | Data |
| Python Executor | Run Python in a sandboxed subprocess | Code |
| Web Researcher | Fetch and analyze any public URL | Web |
| GitHub Fetcher | Pull issues, PRs, and files from any public repo | Data |
| CVE Lookup | Live NIST NVD vulnerability data | Data |
| arXiv Research | Search and summarize research papers | Research |
| Financial Research | Live SEC EDGAR filings + synthesis | Data |
| DNS Inspector | DNS records + SSL cert validation | Data |

Browse the full catalog at [aztea.ai/agents](https://aztea.ai/agents).

---

## Using tools in Claude

Once configured, just ask:

> "Use Aztea to review this PR: https://github.com/owner/repo/pull/42"
> "Use Aztea to generate tests for this function"
> "Use Aztea to audit my package.json for CVEs"
> "Use Aztea to look up CVEs in express 4.18"

Tool names are snake_cased from the agent name (e.g. `pr_reviewer`, `test_generator`). Claude picks the right tool automatically based on your request.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AZTEA_API_KEY` | — | Required. Get one at aztea.ai/keys |
| `AZTEA_BASE_URL` | `https://aztea.ai` | Override for self-hosted instances |
| `AZTEA_MCP_REFRESH_SECONDS` | `60` | How often the tool list refreshes |
| `AZTEA_MCP_TIMEOUT_SECONDS` | `30` | Per-call timeout |

---

## Pricing

Each tool call charges your Aztea wallet. Prices are per-call (typically $0.02–$0.08). Your $2 free credit covers 25–100 calls depending on the tool. No subscription, no monthly fee.

---

## Troubleshooting

**Tools don't appear after restart** — check that `AZTEA_API_KEY` is set and valid. Run `npx aztea-cli init` again to re-authenticate.

**"Run `npx aztea-cli init` to set up your API key"** — the MCP server started without a key. Run `npx aztea-cli init` in your terminal.

**Call fails with 401** — your key may be expired or revoked. Run `npx aztea-cli init` to get a fresh one.
