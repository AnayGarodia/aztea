# Aztea MCP tools — add this to your project's CLAUDE.md

Paste the section below into your project's `CLAUDE.md` (or `~/.claude/CLAUDE.md` for
global use). It tells Claude Code which Aztea tools are available, when to use them, and
pre-authorizes all of them so you never see a permission prompt.

---

## Aztea tools (MCP)

Aztea is installed as an MCP server. Use these tools whenever the task matches — charges
are small ($0.01–0.10/call) and refunded on failure.

| Tool | Use when |
|------|----------|
| `python_code_executor` | Running Python code and seeing real output |
| `multi_file_python_executor` | Running a multi-file Python project with dependencies |
| `linter_agent` | Linting Python/JS/TS without a local toolchain |
| `test_generator` | Generating a runnable test suite for code you just wrote |
| `pr_reviewer` | Reviewing a GitHub PR or diff for bugs and security issues |
| `code_review_agent` | Deep code review with OWASP/CWE findings and copy-paste fixes |
| `dependency_auditor` | Auditing package.json or requirements.txt for CVEs and license issues |
| `cve_lookup_agent` | Looking up CVEs by ID or by package name (live NIST NVD data) |
| `github_file_fetcher` | Fetching files from a public GitHub repository |
| `web_researcher_agent` | Fetching and summarizing a live URL |
| `arxiv_research_agent` | Finding academic papers on arXiv |
| `dns_ssl_inspector` | Checking DNS records, SSL cert expiry, and HTTP headers |
| `changelog_agent` | Getting real changelogs between two versions of a PyPI/npm package |
| `package_finder` | Finding the best library for a task with live download stats |

**Pre-authorize all Aztea tools** (add to `.claude/settings.json` in your project):

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

> **Note:** The exact tool names in `allowedTools` must match what `claude mcp list` shows
> after running `npx aztea-cli init`. Run `claude mcp list` to confirm the names — they are
> derived from each agent's name in lowercase with spaces replaced by underscores.

---

### Quick-start prompts to try

Once Aztea is connected (`claude mcp list` shows `✓ Connected`):

- `"Run this Python script and show me the output"` → python_code_executor
- `"Lint my code and fix the errors"` → linter_agent
- `"Write tests for this function"` → test_generator
- `"Review this PR: https://github.com/owner/repo/pull/42"` → pr_reviewer
- `"Are there any CVEs in express@4.17.1?"` → cve_lookup_agent
- `"Audit my requirements.txt for vulnerabilities"` → dependency_auditor
- `"What changed between requests 2.28 and 2.32?"` → changelog_agent
- `"What's the best Python library for async HTTP with retry?"` → package_finder
- `"Fetch the README from tiangolo/fastapi"` → github_file_fetcher
- `"Check the SSL cert and DNS for aztea.ai"` → dns_ssl_inspector
