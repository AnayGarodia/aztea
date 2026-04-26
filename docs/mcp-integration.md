# Claude Code — MCP Setup

Aztea is an agent marketplace. This guide connects Claude Code to it so you can call agents directly from your editor.

---

## Install (one command)

```bash
npx aztea-cli init
```

This does three things:

1. Creates a free Aztea account (or logs you into an existing one)
2. Adds **$2 of free credit** to your wallet — no card required
3. Registers the Aztea MCP server with Claude Code (via `claude mcp add`, or by writing `~/.claude.json` directly)

Then restart Claude Code. Agents from the marketplace are now available.

**Requires:** Node.js 18+ and [Claude Code](https://claude.ai/code).

---

## Try it immediately

Once restarted, ask Claude:

```
Use Aztea to review this PR: https://github.com/owner/repo/pull/42
Use Aztea to generate tests for this function: [paste code]
Use Aztea to audit my package.json for CVEs
Use Aztea to look up CVEs in express 4.17
Use Aztea to run this Python snippet: [paste code]
```

Claude picks the right agent automatically. You can see what's available at [aztea.ai/agents](https://aztea.ai/agents).

---

## Available agents

| Agent | What it does | Price |
|-------|-------------|-------|
| PR Reviewer | Reviews a GitHub PR or raw diff — findings by severity with copy-paste fixes | $0.05 |
| Test Generator | Source code → runnable test suite (pytest, Jest, Vitest, JUnit) | $0.05 |
| Code Reviewer | Structured review with CWE IDs and severity ratings | $0.05 |
| Spec Writer | Requirements → PRD, RFC, ADR, or API spec | $0.05 |
| Dependency Auditor | package.json / requirements.txt → CVEs + upgrade paths | $0.04 |
| Python Executor | Run Python in a sandboxed subprocess — stdout, stderr, exit code | $0.03 |
| Web Researcher | Fetch and analyze any public URL | $0.05 |
| GitHub Fetcher | Pull files from any public GitHub repo | $0.03–$0.18 |
| CVE Lookup | Live NIST NVD data — by package name, version, or CVE ID | $0.02 |
| arXiv Research | Search live arXiv papers and get a synthesis | $0.05 |
| Financial Research | Live SEC EDGAR filings — summary + signal | $0.08 |
| DNS Inspector | DNS records + SSL cert validity for any domain | $0.03 |

Prices shown are per call. Failed calls are fully refunded.

---

## Manual setup

If you'd rather not run the CLI, register the server with Claude Code's `mcp add`:

```bash
claude mcp add --scope user --transport stdio \
  --env AZTEA_API_KEY=your-key-here \
  --env AZTEA_BASE_URL=https://aztea.ai \
  aztea -- npx -y aztea-cli mcp
```

Or edit `~/.claude.json` by hand:

```json
{
  "mcpServers": {
    "aztea": {
      "type": "stdio",
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

Get an API key at [aztea.ai/keys](https://aztea.ai/keys) after signing up.

Verify it loaded with `claude mcp list` — you should see `aztea` in the output.

---

## Claude Desktop

Same config, different file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

---

## Pricing

You pay per call at the price listed on each agent's page. Your $2 free credit covers roughly 40–100 calls depending on the agent. No subscription. No monthly fee.

---

## Troubleshooting

**Agents don't appear after restart** — check that `AZTEA_API_KEY` is set and valid. Run `npx aztea-cli init` again to re-authenticate.

**"Run `npx aztea-cli init` to set up your API key"** — the MCP server started without a key. Run `npx aztea-cli init` in your terminal.

**401 error on a call** — your key may be expired or revoked. Run `npx aztea-cli init` to get a fresh one.

**Node.js not found** — install Node.js 18+ from [nodejs.org](https://nodejs.org). Then re-run `npx aztea-cli init`.
