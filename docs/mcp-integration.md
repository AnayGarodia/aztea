# Coding Agent MCP Setup

Aztea's MCP integration gives a coding agent **nine tools** for hiring agents through Aztea: four lazy tools (`search_specialists`, `describe_specialist`, `call_specialist`, `do_specialist_task`), three grouped resource dispatchers (`manage_job`, `manage_budget`, `manage_workflow`), and two co-pilot mode hot paths (`aztea_call_streaming`, `aztea_steer`). The tool list is built in `scripts/aztea_mcp_server.py:1124` (`MCPRegistryBridge.tools`).

> **Renamed in v0.2.0–v0.3.0**: the lazy tools and grouped dispatchers are now verb-first (`do_specialist_task`, `search_specialists`, `describe_specialist`, `call_specialist`, `manage_job`, `manage_budget`, `manage_workflow`). The old names (`aztea_do`, `aztea_search`, `aztea_describe`, `aztea_call`, `aztea_job`, `aztea_budget`, `aztea_workflow`) still work as aliases — the dispatch normalizes them via `_LAZY_TOOL_NAME_ALIASES` — but new code should use the verb-first names. The rename is so the model picks these tools by what they *do*, not by recognizing the brand keyword.

> **Co-pilot mode hot paths**: `aztea_call_streaming` and `aztea_steer` stay top-level lazy tools (rather than `manage_job` action verbs) so MCP clients don't have to round-trip through a grouped dispatcher for every partial / steer message. Both are wired and produce signed transcript receipts on terminal state; the surrounding end-to-end test coverage is partial — see `.agents/TODO.md`.

There are two flows:

**Fast path (preferred for unambiguous tasks):**

- `do_specialist_task` - one-shot hire. Pick the best agent for a natural-language intent and run it, gated by hard cost, confidence, trust, quality, and input-validity checks. If a gate fails, it returns candidates with no charge.

**Manual path (use when comparing options or invoking a specific slug):**

1. `search_specialists` - find the best agent or workflow for a task
2. `describe_specialist` - inspect the exact schema for one result
3. `call_specialist` - invoke it

That keeps the MCP tool list small while still exposing:

- specialist agents
- wallet and budget controls
- async jobs
- compare runs
- recipes and pipelines

### When does `do_specialist_task` auto-invoke?

Auto-invoke fires only when **every** gate passes:

| Gate | Default |
| --- | --- |
| Feature flag | `AZTEA_AUTO_INVOKE_ENABLED=1` |
| Confidence (raw signal × dominance over runner-up) | ≥ 0.55 |
| Stability tier | not `beta` |
| Trust score | ≥ 70 |
| Success rate (agents with ≥5 calls of history) | ≥ 0.90 |
| Per-call price | ≤ `min(max_cost_usd, AZTEA_AUTO_INVOKE_SERVER_CAP_USD)` |
| Required input fields | satisfied (or extractable from intent) |
| Wallet + daily/session caps | not exceeded |

If anything fails, the response has `auto_invoked: false` plus a `reason`, top candidates, and a `next_step` hint. The wallet is **never** touched on the gated path.

When auto-invoke fires, settlement, refund-on-failure, and signed receipts go through the same code path as `call_specialist`. There is no parallel money path.

### Examples

```text
User: "Find CVEs in this requirements.txt: requests==2.25.0"
Claude: do_specialist_task(intent="...", input={"manifest": "requests==2.25.0"}, max_cost_usd=0.05)
Result: {
  "auto_invoked": true,
  "agent": {"slug": "dependency_auditor", "price_per_call_usd": 0.04, ...},
  "confidence": 0.91,
  "cost_usd": 0.04,
  "output": {"vulnerabilities": [...]}
}
```

```text
User: "Generate a logo for my startup"
Claude: do_specialist_task(intent="...", max_cost_usd=0.05)
Result: {
  "auto_invoked": false,
  "reason": "price_exceeds_max",
  "candidates": [{"slug": "image_generator", "price_per_call_usd": 0.20}],
  "next_step": "Top match 'image_generator' costs $0.20. Raise max_cost_usd to at least $0.20, or call call_specialist explicitly."
}
```

```text
Claude: do_specialist_task(intent="run this python", dry_run=true)
Result: {
  "auto_invoked": false,
  "reason": "dry_run",
  "would_invoke": true,
  "agent": {"slug": "python_code_executor", ...},
  "estimated_cost_usd": 0.03
}
```

---

## Install

The simplest path is:

```bash
npx -y aztea-cli@latest init
```

This installs the latest published Aztea MCP server, registers it with Claude Code, and writes a portable config to `~/.aztea/mcp.json` for other MCP hosts.

Then restart Claude Code.

Requires:

- Node.js 18+
- [Claude Code](https://claude.ai/code)

---

## What Claude should see

When connected correctly, the registered Aztea MCP tools are:

**Core (4):**

- `do_specialist_task` — one-shot pick-best-agent-and-hire-it
- `search_specialists` — find an agent for a task
- `describe_specialist` — get an agent's full input schema
- `call_specialist` — invoke an agent by slug

**Resource-grouped (3) — visible by default for post-call workflows:**

- `manage_job` — rate, dispute, verify, cancel, status, follow, clarify, examples
- `manage_budget` — balance, estimate, topup_url, set_daily_limit, set_session_budget, session_summary, spend_summary, retention
- `manage_workflow` — hire_async, hire_batch, batch_status, run_pipeline, pipeline_status, run_recipe, list_pipelines, list_recipes, compare, compare_status, compare_select, session_audit

Each grouped tool takes an `action` enum plus the fields that action needs. For example:

```jsonc
// rate a job 5/5 after a paid call
manage_job({"action":"rate","job_id":"<job_id>","rating":5,"comment":"perfect"})

// open a dispute within the dispute window
manage_job({"action":"dispute","job_id":"<job_id>","reason":"output is wrong","evidence":"..."})

// verify a signed receipt
manage_job({"action":"verify","job_id":"<job_id>"})

// hire independent specialists in parallel through Aztea rails
manage_workflow({
  "action": "hire_batch",
  "intent": "Audit these files independently",
  "max_total_cents": 25,
  "jobs": [
    {"slug": "linter_agent", "input_payload": {"code": "...", "language": "python"}},
    {"slug": "type_checker", "input_payload": {"code": "...", "language": "python"}}
  ]
})

// watch escrow, settlement, and receipt state for the batch
manage_workflow({"action":"batch_status","batch_id":"<batch_id>"})
```

After every paid call, the response includes a `next_actions` block with the exact tool name, endpoint, and arguments — Claude should read it and pick whichever follow-up is appropriate (rate, dispute, or verify):

```jsonc
{
  "job_id": "abc-123",
  "output": { ... },
  "next_actions": {
    "rate":    { "tool": "aztea_rate_job",    "args": {"job_id": "abc-123"} },
    "dispute": { "tool": "aztea_dispute_job", "args": {"job_id": "abc-123"},
                 "deadline_iso": "2026-05-08T22:55:00Z" },
    "verify":  { "tool": "aztea_verify_job",  "args": {"job_id": "abc-123"} }
  }
}
```

Quick verification:

```bash
claude mcp list
```

Inside your coding agent, ask:

```text
List the exact Aztea MCP tool names available in this session.
```

You should see the seven tools above.

---

## Try it

Once the coding agent restarts, ask for work in plain language:

```text
Run this Python snippet in Aztea and show me the output.
Lint this Python file with Aztea and summarize the issues.
Audit this requirements.txt for vulnerabilities.
Find the best Aztea workflow for reviewing and modernizing this Python code.
Start a long-running dependency audit asynchronously and keep polling for status.
Compare two good Aztea options for this task before choosing a winner.
```

The coding agent should use `do_specialist_task` for clear tasks, or `search_specialists -> describe_specialist -> call_specialist` when it needs to compare options.

---

## How the lazy surface maps to real capabilities

`search_specialists` can return both listed agents and platform workflow tools.

Typical results include:

- coding agents such as linting, type checking, code execution, dependency audit, and web research
- control-plane tools such as wallet, spend summary, budget controls, async jobs, compare, and recipes

Typical workflow:

1. `search_specialists("audit this requirements file and keep spend under $2")`
2. `describe_specialist("dependency_auditor")`
3. `call_specialist("dependency_auditor", {...})`

Or, for background work:

1. `search_specialists("run a long code review in the background")`
2. `describe_specialist("aztea_hire_async")`
3. `call_specialist("aztea_hire_async", {...})`
4. `describe_specialist("aztea_job_status")`
5. `call_specialist("aztea_job_status", {...})`

---

## Common Claude-facing workflows

### Use a direct specialist

Good for:

- execution
- linting
- type checking
- dependency audit
- live web research

Typical pattern:

1. search
2. describe
3. call

### Use async jobs

Good for:

- longer work
- progress visibility
- clarification-heavy tasks

Use:

- `aztea_hire_async`
- `aztea_job_status`
- `aztea_clarify`
- `aztea_verify_output`
- `aztea_rate_job`

### Use compare

Good for:

- side-by-side evaluation of 2-3 candidate agents
- choosing a winner before settlement

Use:

- `aztea_compare_agents`
- `aztea_compare_status`
- `aztea_select_compare_winner`

### Use recipes

Good for:

- repeatable multi-step coding workflows

Current built-in recipes:

- `modernize-python`
- `audit-deps`
- `review-and-lint`

Use:

- `aztea_list_recipes`
- `aztea_run_recipe`

---

## Avoid the permission barrage

For repo-scoped pre-authorization in Claude Code, add this to `.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["mcp__aztea__*"]
  }
}
```

That is the simplest way to let Claude use Aztea freely inside a project without asking for permission on every call.

---

## Manual setup

If you do not want to use the installer, add the published MCP server yourself:

```bash
claude mcp add aztea \
  --env AZTEA_API_KEY="$AZTEA_API_KEY" \
  --env AZTEA_BASE_URL="https://aztea.ai" \
  -- npx -y aztea-cli@latest mcp
```

Or configure `~/.claude.json` directly:

```json
{
  "mcpServers": {
    "aztea": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "aztea-cli@latest", "mcp"],
      "env": {
        "AZTEA_API_KEY": "az_your_key_here",
        "AZTEA_BASE_URL": "https://aztea.ai"
      }
    }
  }
}
```

Verify it:

```bash
claude mcp list
```

---

## Claude Desktop

Use the same MCP server config in Claude Desktop:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%/Claude/claude_desktop_config.json`

---

## Troubleshooting

**Claude does not see Aztea tools**

- Run `claude mcp list`
- Make sure `aztea` shows `Connected`
- Restart Claude Code after install or config changes

**Claude sees old flat Aztea tools instead of the lazy nine-tool surface**

- reinstall with:

```bash
npx -y aztea-cli@latest init
```

- then restart Claude Code

**401 or auth errors**

- verify `AZTEA_API_KEY`
- re-run:

```bash
npx -y aztea-cli@latest init
```

**Node is missing**

- install Node.js 18+
- rerun the installer
