# Aztea MCP for Claude Code

Paste this into your project's `CLAUDE.md` or `~/.claude/CLAUDE.md`.

It tells Claude Code how to use Aztea efficiently without wasting tokens on the wrong discovery pattern or reissuing paid work unnecessarily.

---

## Aztea MCP usage

Aztea is available as an MCP server. Treat it as a specialist labor market plus a workflow control plane for coding agents.

> **Tool names**: the four discovery/hire tools `do_specialist_task`, `search_specialists`, `describe_specialist`, `call_specialist`, plus the grouped dispatchers `manage_job`, `manage_budget`, `manage_workflow`, plus the admin-only observability tools `aztea_status`, `aztea_inspect`, `aztea_query`. The old flat names `aztea_do` / `aztea_search` / `aztea_describe` / `aztea_call` / `aztea_job` / `aztea_budget` / `aztea_workflow` still resolve via dispatch aliases for backward compatibility.

For Claude Code, the intended MCP flow is:

1. `do_specialist_task` - hire the best specialist for clear tasks under hard cost, confidence, trust, quality, and input-validity gates
2. `search_specialists` - find the best agent or workflow for ambiguous tasks
3. `describe_specialist` - inspect the exact schema for one result
4. `call_specialist` - invoke it explicitly

The lazy ten-tool surface is deliberate. Do not expect every Aztea capability to be a separate top-level MCP tool — control-plane operations (estimate, async hire, batch, compare, recipes, pipelines, ratings, disputes, budgets) are reached through the grouped `manage_job` / `manage_budget` / `manage_workflow` dispatchers via an `action` verb, not separate tools.

---

## Default operating rules

- Use Aztea proactively when external execution, live data, delegation, or workflow orchestration would help.
- The user does not need to say "use Aztea." Decide when a specialist hire is useful and explain the receipt/settlement result afterward.
- Prefer `do_specialist_task` for a clear first hire. It returns no-charge recommendations if price, trust, confidence, or inputs fail a gate.
- Before expensive or unfamiliar work, estimate cost with `manage_budget(action="estimate", slug="<slug>")`, then run it.
- For long-running or background work, prefer `manage_workflow(action="hire_async")`, then poll with `manage_job(action="status")`.
- If a job asks for clarification, respond with `manage_job(action="clarify")` instead of starting over.
- After async completion, use `manage_job(action="verify_output")`, then `manage_job(action="rate")`. Use `manage_job(action="dispute")` only for materially wrong output.
- For many independent subtasks, prefer `manage_workflow(action="hire_batch")` over serial single calls. Use it when work splits by file, package, endpoint, test case, or specialist role.
- After a batch hire, tell the user Aztea opened parallel marketplace hires, then poll `batch_id` with `manage_workflow(action="batch_status")` and summarize escrow, settlement, job IDs, and receipt state from `parallel_hire_trace`.
- For side-by-side evaluation of 2-3 options, use `manage_workflow(action="compare")`, then `manage_workflow(action="compare_status")`, then `manage_workflow(action="compare_select")`.
- For repeatable multi-step work, check `manage_workflow(action="list_recipes")` or `manage_workflow(action="list_pipelines")` first.
- Do not create a second compare, recipe, or pipeline run just to check status. Use the matching status tool.

---

## High-value Aztea control-plane operations

These are NOT marketplace agents — they are control-plane operations reached through the grouped dispatchers (`manage_budget`, `manage_workflow`, `manage_job`) with an `action` verb:

| Dispatcher call | Use when |
|-----------------|----------|
| `manage_budget(action="balance")` | Check available credit before paid work |
| `manage_budget(action="session_summary")` | Check current session spend and remaining budget |
| `manage_budget(action="set_session_budget")` | Cap spend for the current Claude session |
| `manage_budget(action="estimate", slug="<slug>")` | Preview cost and latency before hiring |
| `manage_workflow(action="hire_async")` | Start background work |
| `manage_job(action="status")` | Poll async jobs and read progress / clarification requests |
| `manage_job(action="clarify")` | Answer an agent clarification request |
| `manage_job(action="verify_output")` | Accept or reject output inside the verification window |
| `manage_job(action="rate")` | Rate a completed job |
| `manage_job(action="dispute")` | File a dispute for materially bad output |
| `manage_workflow(action="hire_batch")` | Hire independent specialists in parallel under one batch rail |
| `manage_workflow(action="compare")` | Run 2-3 agents on the same task |
| `manage_workflow(action="compare_status")` | Poll an existing compare session |
| `manage_workflow(action="compare_select")` | Finalize the chosen compare result |
| `manage_workflow(action="list_recipes")` | Discover built-in workflow templates |
| `manage_workflow(action="run_recipe")` | Execute a built-in workflow template |
| `manage_workflow(action="list_pipelines")` | Discover saved pipelines |
| `manage_workflow(action="run_pipeline")` | Execute a saved pipeline |
| `manage_workflow(action="pipeline_status")` | Poll an existing pipeline or recipe run |

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

- `audit-deps`
- `domain-health`

If you do not know the recipe ID, search for recipe or workflow first, or call `manage_workflow(action="list_recipes")`.

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
