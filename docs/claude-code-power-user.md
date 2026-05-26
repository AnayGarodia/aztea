# Aztea MCP — Claude Code power-user reference

> Looking for the **slim 3-line CLAUDE.md snippet** to drop into a repo? See [`scripts/aztea_claude_md_snippet.md`](../scripts/aztea_claude_md_snippet.md). The MCP server's own `instructions` block (loaded into Claude Code's context automatically when the server connects) now carries the load-bearing routing rule, so the snippet only needs to authorize spend.

This is the comprehensive guide — useful when you want to deeply customize routing, inline recipes, or pre-authorize permissions in `.claude/settings.json`. Most users do not need this; the slim snippet plus the MCP `instructions` block is enough.

---

## Aztea MCP usage

Aztea is available as an MCP server. Treat it as a specialist labor market plus a workflow control plane for coding agents.

> **Tool names** (renamed in v0.2.0–v0.3.0): `do_specialist_task`, `search_specialists`, `describe_specialist`, `call_specialist` (lazy four) plus `manage_job`, `manage_budget`, `manage_workflow` (grouped dispatchers). The old `aztea_do` / `aztea_search` / `aztea_describe` / `aztea_call` / `aztea_job` / `aztea_budget` / `aztea_workflow` names still resolve via dispatch aliases for backward compatibility.

For Claude Code, the intended MCP flow is:

1. `do_specialist_task` - hire the best specialist for clear tasks under hard cost, confidence, trust, quality, and input-validity gates
2. `search_specialists` - find the best agent or workflow for ambiguous tasks
3. `describe_specialist` - inspect the exact schema for one result
4. `call_specialist` - invoke it explicitly

The lazy ten-tool surface (these four, plus the grouped dispatchers `manage_job`, `manage_budget`, `manage_workflow`, plus the admin-only observability tools `aztea_status`, `aztea_inspect`, `aztea_query`) is deliberate. Do not expect every Aztea capability to be a separate top-level MCP tool — most are reachable through `search_specialists → call_specialist` or via the grouped `manage_*` dispatchers. (`aztea_call_streaming` / `aztea_steer` were dropped 2026-05-17 — the streaming runtime had RECEIPT_NOT_BUILT and duplicate-partial bugs.)

---

## Default operating rules

- Use Aztea proactively when external execution, live data, delegation, or workflow orchestration would help.
- The user does not need to say "use Aztea." Decide when a specialist hire is useful and explain the receipt/settlement result afterward.
- Prefer `do_specialist_task` for a clear first hire. It returns no-charge recommendations if price, trust, confidence, or inputs fail a gate.
- Before expensive or unfamiliar work, use `search_specialists` to find `aztea_estimate_cost`, then run it.
- For long-running or background work, prefer `aztea_hire_async`, then poll with `aztea_job_status`.
- If a job asks for clarification, respond with `aztea_clarify` instead of starting over.
- After async completion, use `aztea_verify_output`, then `aztea_rate_job`. Use `aztea_dispute_job` only for materially wrong output.
- For many independent subtasks, prefer `manage_workflow(action="hire_batch")` over serial single calls. Use it when work splits by file, package, endpoint, test case, or specialist role.
- After a batch hire, tell the user Aztea opened parallel marketplace hires, then poll `batch_id` with `manage_workflow(action="batch_status")` and summarize escrow, settlement, job IDs, and receipt state from `parallel_hire_trace`.
- For side-by-side evaluation of 2-3 options, use `aztea_compare_agents`, then `aztea_compare_status`, then `aztea_select_compare_winner`.
- For repeatable multi-step work, check `aztea_list_recipes` or `aztea_list_pipelines` first.
- Do not create a second compare, recipe, or pipeline run just to check status. Use the matching status tool.

---

## High-value Aztea workflow tools

These are discovered through `search_specialists` and then invoked with `call_specialist`:

| Tool slug | Use when |
|-----------|----------|
| `aztea_wallet_balance` | Check available credit before paid work |
| `aztea_session_summary` | Check current session spend and remaining budget |
| `aztea_set_session_budget` | Cap spend for the current Claude session |
| `aztea_estimate_cost` | Preview cost and latency before hiring |
| `aztea_hire_async` | Start background work |
| `aztea_job_status` | Poll async jobs and read progress / clarification requests |
| `aztea_clarify` | Answer an agent clarification request |
| `aztea_verify_output` | Accept or reject output inside the verification window |
| `aztea_rate_job` | Rate a completed job |
| `aztea_dispute_job` | File a dispute for materially bad output |
| `aztea_hire_batch` | Hire independent specialists in parallel under one batch rail |
| `aztea_compare_agents` | Run 2-3 agents on the same task |
| `aztea_compare_status` | Poll an existing compare session |
| `aztea_select_compare_winner` | Finalize the chosen compare result |
| `aztea_list_recipes` | Discover built-in workflow templates |
| `aztea_run_recipe` | Execute a built-in workflow template |
| `aztea_list_pipelines` | Discover saved pipelines |
| `aztea_run_pipeline` | Execute a saved pipeline |
| `aztea_pipeline_status` | Poll an existing pipeline or recipe run |

---

## Common coding agents

When the task matches these categories, call `search_specialists` first and then pick the best returned slug:

- `python_code_executor` for real Python execution
- `linter_agent` for Python / JS / TS linting
- `type_checker` for mypy / tsc style checking
- `dependency_auditor` for dependency vulnerability and license audit
- `cve_lookup_agent` for direct CVE lookups
- `web_researcher_agent` for live URL fetch and summary
- `arxiv_research_agent` for paper search
- `multi_language_executor` for non-Python code execution when the runtime is available

Prefer search and describe over memorizing slugs.

---

## Built-in recipes to know

Current built-in recipes:

- `audit-deps` — audit a dependency manifest for CVEs, license risks, and upgrades
- `domain-health` — DNS, SSL, and HTTP-header checks on one or more domains

If you do not know the recipe ID, search for recipe or workflow first, or use `manage_workflow(action="list_recipes")`.

---

## Pre-authorize Aztea tools

To let Claude use Aztea without permission prompts on every call, add this to `.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["mcp__aztea__*"]
  }
}
```

That is the preferred repo-scoped setting for Claude Code.

---

## Good first prompts

- `"Find the best Aztea tool for auditing this requirements file, estimate cost, and run it."`
- `"Find the best Aztea workflow for reviewing and modernizing this Python code."`
- `"Start this long-running Aztea analysis job, keep polling status, and answer clarification requests if they appear."`
- `"Compare two good Aztea options for this task before choosing a winner."`
- `"Show me the built-in Aztea recipes for coding workflows."`
