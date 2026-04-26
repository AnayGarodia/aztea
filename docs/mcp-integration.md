# Claude Code — MCP Setup

Aztea gives Claude Code a catalog of pay-per-call tools: code execution, linting, test generation, CVE lookups, package research, and more. One install, one key, no API accounts to manage.

---

## Install (one command)

```bash
npx aztea-cli init
```

This does three things:

1. Creates a free Aztea account (or logs you into an existing one)
2. Adds **$2 of free credit** to your wallet — no card required
3. Registers the Aztea MCP server with Claude Code (via `claude mcp add`, or by writing `~/.claude.json` directly)

Then restart Claude Code. All tools from the catalog are now available.

**Requires:** Node.js 18+ and [Claude Code](https://claude.ai/code).

---

## Try it immediately

Once restarted, ask Claude:

```
Run this Python script and show me the output
Lint my code and fix the errors
Write tests for this function
Review this PR: https://github.com/owner/repo/pull/42
Are there any CVEs in express@4.17.1?
Audit my requirements.txt for vulnerabilities
What changed between requests 2.28 and 2.32?
What's the best async HTTP library for Python?
Fetch the README from tiangolo/fastapi
Check the SSL cert for aztea.ai
```

Claude picks the right tool automatically. Each result includes a `cost_usd` field showing exactly what was charged.

---

## Tool catalog

| Tool | Use when | Price |
|------|----------|-------|
| Python Code Executor | Running Python code and seeing real output | $0.03/run |
| Multi-File Python Executor | Running a multi-file project with dependencies | $0.03/run |
| Linter Agent | Linting Python/JS/TS without a local toolchain (ruff for Python) | $0.01/file |
| Test Generator | Source code → runnable test suite (pytest, Jest, Vitest, JUnit) | $0.05/call |
| PR Reviewer | GitHub PR or diff → structured findings by severity with copy-paste fixes | $0.05/call |
| Code Reviewer | Deep code review with CWE IDs, OWASP, and copy-paste fixes | $0.05/call |
| Dependency Auditor | package.json / requirements.txt → CVEs (live NVD) + upgrade paths | $0.04/call |
| CVE Lookup | Live NIST NVD data — by package name, version, or CVE ID | $0.01–$0.06 |
| GitHub File Fetcher | Files from any public GitHub repo (auto-detects default branch) | $0.03–$0.18 |
| Web Researcher | Fetch and analyze any public URL (up to 10 at once) | $0.02–$0.15 |
| arXiv Research | Search live arXiv papers and get a synthesis | $0.05/call |
| DNS & SSL Inspector | DNS records + SSL cert expiry + HTTP headers for any domain | $0.04–$0.16 |
| Changelog Agent | Real changelogs between two PyPI or npm package versions | $0.02/call |
| Package Finder | Best library for a task with live download stats and LLM ranking | $0.02/call |

Failed calls are fully refunded. Prices shown are per call at the base tier.

---

## Skip the permission prompt

By default Claude Code asks for permission before each MCP tool call. To pre-authorize all Aztea tools for a project, add this to `.claude/settings.json` in your project root:

```json
{
  "allowedTools": [
    "mcp__aztea__python_code_executor",
    "mcp__aztea__multi_file_python_executor",
    "mcp__aztea__linter_agent",
    "mcp__aztea__test_generator",
    "mcp__aztea__pr_reviewer",
    "mcp__aztea__code_review_agent",
    "mcp__aztea__dependency_auditor",
    "mcp__aztea__cve_lookup_agent",
    "mcp__aztea__github_file_fetcher",
    "mcp__aztea__web_researcher_agent",
    "mcp__aztea__arxiv_research_agent",
    "mcp__aztea__dns_ssl_inspector",
    "mcp__aztea__changelog_agent",
    "mcp__aztea__package_finder"
  ]
}
```

Run `claude mcp list` after connecting to see the exact tool names — they are derived from the agent name in lowercase with spaces replaced by underscores.

---

## Manual setup

If you'd rather not run the CLI, register the server with Claude Code's `mcp add`:

```bash
claude mcp add --scope user --transport stdio \
  aztea \
  -e AZTEA_API_KEY=your-key-here \
  -e AZTEA_BASE_URL=https://aztea.ai \
  -- node ~/.aztea/node_modules/aztea-cli/src/mcp-server.js
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

Verify it loaded with `claude mcp list` — you should see `✓ Connected` next to `aztea`.

---

## Claude Desktop

Same config, different file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

---

## Pricing

You pay per call at the price listed on each tool's page. Your $2 free credit covers roughly 40–200 calls depending on the tool. No subscription. No monthly fee. Failed calls are always refunded.

---

## Troubleshooting

**Tools don't appear after restart** — check that `AZTEA_API_KEY` is set and valid. Run `npx aztea-cli init` again to re-authenticate.

**"Run `npx aztea-cli init` to set up your API key"** — the MCP server started without a key. Run `npx aztea-cli init` in your terminal.

**`✗ Failed to connect` in `claude mcp list`** — run `npx aztea-cli init` to reinstall and re-register. This also updates the server to the latest version.

**401 error on a call** — your key may be expired or revoked. Run `npx aztea-cli init` to get a fresh one.

**Node.js not found** — install Node.js 18+ from [nodejs.org](https://nodejs.org). Then re-run `npx aztea-cli init`.
