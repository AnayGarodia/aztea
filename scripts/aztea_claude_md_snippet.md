# Aztea MCP for Claude Code

Paste the section below into your project's `CLAUDE.md` or `~/.claude/CLAUDE.md`.
It tells Claude Code how to use Aztea without wasting tokens on tool discovery or
re-running paid work by accident.

---

## Aztea MCP usage

Aztea is installed as an MCP server. Treat it as a specialist agent marketplace with
cost controls, async jobs, compare mode, and reusable workflows.

If the MCP server is started with `AZTEA_LAZY_MCP_SCHEMAS=1`, prefer the slim flow:

- `aztea_search` to find candidate tools
- `aztea_describe` to inspect one tool's exact schema
- `aztea_call` to invoke it

This keeps the tool list small and reduces Claude's token burn on first contact.

### Default operating rules

- Before expensive or unfamiliar work, call `aztea_estimate_cost`.
- When you do not know which agent to use, call `aztea_discover`.
- For long-running or clarification-heavy work, use `aztea_hire_async` and then poll with `aztea_job_status`.
- If a job asks for clarification, answer with `aztea_clarify` instead of starting over.
- After async completion, use `aztea_verify_output`, then `aztea_rate_job`. Use `aztea_dispute_job` if the output is materially wrong.
- For 2-3 candidate agents on the same task, use `aztea_compare_agents`, then poll with `aztea_compare_status`, then finalize with `aztea_select_compare_winner`.
- For reusable multi-step workflows, call `aztea_list_recipes` or `aztea_list_pipelines` first, then execute with `aztea_run_recipe` or `aztea_run_pipeline`, and poll with `aztea_pipeline_status` if needed.
- Do not call `aztea_compare_agents`, `aztea_run_pipeline`, or `aztea_run_recipe` again just to poll status. Use their dedicated status tools.

### High-value Aztea meta-tools

| Tool | Use when |
|------|----------|
| `aztea_wallet_balance` | Check available credit before paid work |
| `aztea_session_summary` | Check current session spend and remaining budget |
| `aztea_set_session_budget` | Cap spend for the current Claude session |
| `aztea_estimate_cost` | Preview cost and latency before hiring |
| `aztea_discover` | Find the best agent for a task |
| `aztea_get_examples` | Inspect real example outputs before hiring |
| `aztea_hire_async` | Start long-running work |
| `aztea_job_status` | Poll async jobs and read progress / clarification requests |
| `aztea_clarify` | Answer an agent's clarification request |
| `aztea_verify_output` | Accept or reject output within the verification window |
| `aztea_rate_job` | Submit a post-job rating |
| `aztea_dispute_job` | File a dispute for materially bad output |
| `aztea_hire_batch` | Launch many independent jobs in one request |
| `aztea_compare_agents` | Run 2-3 agents on the same task |
| `aztea_compare_status` | Poll an existing compare session |
| `aztea_select_compare_winner` | Pay only the chosen compare winner |
| `aztea_list_recipes` | Discover built-in workflow templates |
| `aztea_run_recipe` | Execute a built-in workflow template |
| `aztea_list_pipelines` | Discover saved pipelines visible to you |
| `aztea_run_pipeline` | Execute a saved pipeline |
| `aztea_pipeline_status` | Poll an existing pipeline or recipe run |

### Common built-in coding agents

| Tool | Use when |
|------|----------|
| `python_code_executor` | Run Python code and inspect real output |
| `multi_file_python_executor` | Run a multi-file Python project with dependencies |
| `linter_agent` | Lint Python / JS / TS without a local toolchain |
| `test_generator` | Generate runnable tests for code you just wrote |
| `pr_reviewer` | Review a GitHub PR or diff |
| `code_review_agent` | Do a deeper code review with concrete fixes |
| `dependency_auditor` | Audit dependencies for CVEs and license risk |
| `cve_lookup_agent` | Look up CVEs by ID or package name |
| `github_file_fetcher` | Fetch files from a public GitHub repo |
| `web_researcher_agent` | Read and summarize a live URL |
| `arxiv_research_agent` | Find relevant arXiv papers |
| `dns_ssl_inspector` | Check DNS, SSL expiry, and security headers |
| `changelog_agent` | Compare changelogs across package versions |
| `package_finder` | Find a good library for a task |

### Built-in recipes to know

- `review-and-test`: code review, then test generation
- `audit-deps`: dependency audit, then package suggestions
- `modernize-python`: lint-focused modernization pass

### Pre-authorize Aztea tools

Add these to `.claude/settings.json` so Claude can use them without permission prompts:

```json
{
  "allowedTools": [
    "mcp__aztea__aztea_wallet_balance",
    "mcp__aztea__aztea_spend_summary",
    "mcp__aztea__aztea_set_daily_limit",
    "mcp__aztea__aztea_topup_url",
    "mcp__aztea__aztea_session_summary",
    "mcp__aztea__aztea_set_session_budget",
    "mcp__aztea__aztea_estimate_cost",
    "mcp__aztea__aztea_list_recipes",
    "mcp__aztea__aztea_list_pipelines",
    "mcp__aztea__aztea_hire_async",
    "mcp__aztea__aztea_job_status",
    "mcp__aztea__aztea_clarify",
    "mcp__aztea__aztea_rate_job",
    "mcp__aztea__aztea_dispute_job",
    "mcp__aztea__aztea_verify_output",
    "mcp__aztea__aztea_discover",
    "mcp__aztea__aztea_get_examples",
    "mcp__aztea__aztea_hire_batch",
    "mcp__aztea__aztea_compare_agents",
    "mcp__aztea__aztea_compare_status",
    "mcp__aztea__aztea_select_compare_winner",
    "mcp__aztea__aztea_run_pipeline",
    "mcp__aztea__aztea_pipeline_status",
    "mcp__aztea__aztea_run_recipe",
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

The exact prefix may differ depending on the MCP server alias. Confirm with `claude mcp list`.

### Good first prompts

- `"Find the best Aztea agent for reviewing this Python diff, estimate cost, and run it asynchronously."`
- `"Show me the built-in Aztea recipes for coding workflows."`
- `"Run the review-and-test recipe on this code."`
- `"Compare 2-3 good dependency-audit agents on this requirements set, then let me choose the winner."`
- `"Start this long-running analysis job, keep polling status, and answer any clarification requests if they appear."`
