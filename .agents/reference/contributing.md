# Contributing — dev commands, agents, shards, env

> Resolved reference for `CLAUDE.md`. Read when running the suite, adding an agent, editing a shard, or setting up local env.

## Dev commands

```bash
# Backend
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000 --reload

# Docker dev (SQLite at ./data/registry.db)
cp .env.example .env && make docker

# Frontend
cd frontend && npm install && npm run dev

# Reproduce production behaviour locally before deploy:
cd frontend && npm run build && npx vite preview --port 4173
# If `vite preview` works but prod doesn't, the bug is in the Caddy → uvicorn → SPA-fallback path or a route definition shadowing the SPA.

# Tests — main suite (run the SDK contract suite separately — it can segfault
# under Python 3.14 on macOS). Hypothesis is pinned (requirements-dev.txt) so
# the property suite collects cleanly:
pytest -q tests --ignore=tests/test_sdk_contract.py
pytest -q tests/test_sdk_contract.py

# Integration tests only (covered by the main suite)
pytest -q tests/integration

# Line-budget enforcement (every Python source file < 1000 lines)
python scripts/check_file_line_budget.py

# Single integration test
pytest tests/integration/test_workers_jobs_core.py::test_worker_claim_heartbeat_and_complete_with_owner_auth -q

# Frontend prod build
cd frontend && npm run build

# Manual DB migration
python -m core.migrate

# MCP server (stdio) — preferred entrypoints
aztea mcp serve              # CLI wrapper
python -m aztea.mcp.server   # module form
python scripts/aztea_mcp_server.py   # legacy compat shim (still works)

# Run ledger reconciliation
curl -H "Authorization: Bearer $API_KEY" -X POST http://localhost:8000/ops/payments/reconcile
```

**Current test status:** `pytest --collect-only` reports **4674 tests collected** under `tests/` (excluding `tests/test_sdk_contract.py`) as of 2026-05-20. Property tests (`tests/property/`) collect cleanly now that Hypothesis is pinned (PR #47, 2026-05-15). The SDK contract suite can still segfault on Python 3.14 macOS and is excluded by the canonical command above. Re-anchor this line (collected + passed + skipped + date) when you next run the suite end-to-end.

---

## Operational runbooks

Runbooks for operational scenarios live in `docs/runbooks/`:

- **`docs/runbooks/deploy.md`** — production deploy process, nginx config, prod env vars, package distribution, Stripe webhook setup
- **`docs/runbooks/ledger-drift.md`** — what to do when reconciliation reports non-zero drift; step-by-step query guide
- **`docs/runbooks/runtime-prerequisites.md`** — which agents require which system packages (Playwright/chromium, Node, Deno, Go, Rust, ruff, mypy, tsc) and how to verify
- **`docs/runbooks/buyer-surface-smoke-test.md`** — ordered smoke-test checklist to verify all buyer surfaces (web, MCP/Claude, Python SDK, CLI, TUI, REST) after a deploy

Update the relevant runbook in the same commit as any change that affects money flows, adds a runtime dependency, or changes a buyer surface.

---

## Adding a new built-in agent

1. Create `agents/{slug}.py` with a `run(payload: dict) -> dict` function and a module-level docstring describing inputs, outputs, external dependencies, and runtime requirements.
2. Generate a stable ID: `uuid.uuid5(uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8'), 'aztea.builtin.{slug}')`.
3. Add the ID as a constant in `server/builtin_agents/constants.py` and wire into `BUILTIN_INTERNAL_ENDPOINTS` + `CURATED_PUBLIC_BUILTIN_AGENT_IDS` (only if the agent performs real external work beyond pure LLM prompting).
4. Add the agent import to `server/application_parts/part_000.py` (the import shard).
5. Add a case to `_execute_builtin_agent()` — `grep -n "_execute_builtin_agent" server/application_parts/part_*.py` to find it.
6. Add a spec entry to `server/builtin_agents/specs_part1.py` **or** `specs_part2.py` (keep each under ~900 lines). The final curated list is assembled by `server/builtin_agents/specs.py::builtin_agent_specs()`.
7. Return a structured error envelope on failure — `{"error": {"code": "...", "message": "..."}}` — not a raw exception.
8. Handle the no-LLM case: if the agent fetches real data then synthesises with an LLM, it must return the raw data if LLM synthesis fails rather than raising.
9. Run `pytest tests/integration/test_hooks_builtin_mcp.py -q` to confirm registration + MCP manifest pick up the new agent.

**Agents earn a place in the public marketplace by doing something Claude can't do in a chat session.** Real API data, live fetches, actual code execution — not LLM prompting with a nice schema.

### Adding a third-party agent (community / external)

Built-in agents follow the steps above. Community contributors who want to
list a new agent on Aztea **without** a server-side change use the
`aztea publish <path>` CLI:

- `*.skill.md` → hosted on Aztea (`POST /skills`), auto-approved at the DB layer.
- `agent.md` → author-hosted external endpoint (`POST /onboarding/ingest`).
- `*.py` with `def handler(payload)` → author-hosted endpoint (`POST /registry/register` + `--endpoint <URL>`).

The CLI runs a verification gate (`core/listing_safety.py`) before any
registration: prompt-injection / API-key / blocked-import scans, near-clone
detection, SSRF + Aztea-host check. Server re-runs the same scan on
`/skills`, `/registry/register`, and `/onboarding/ingest` so direct API
clients can't bypass it. Non-master registrations land in
`review_status='probation'` (live and callable; auto-invoke is rank-
penalised and price-capped at $1.00 until track record graduates them to
`'approved'`).

### Editing a shard (`server/application_parts/part_NNN.py`)

The shards share a single logical namespace — `server/application.py` compiles each shard in order into its own module globals. Practical rules:

- Add new imports to **`part_000.py`** (the import shard); other shards reference symbols already in scope.
- Add new top-level routes at the end of the shard that naturally owns the concern.
- Keep each shard **< 900 lines**. CI fails on any file > 1000 lines.
- If a function grows too large, move it into a helper module under `core/` — do **not** re-split the shards by hand.
- Every shard begins with a `# server.application shard N — <what it owns>` comment.

---

## Required env vars (minimum to run locally)

```
API_KEY=                     # master API key
GROQ_API_KEY=                # or any other LLM provider key
SERVER_BASE_URL=http://localhost:8000
```

Optional but useful locally:

```
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
ALLOW_PRIVATE_OUTBOUND_URLS=1   # dev only — allows localhost agent endpoints
AZTEA_LLM_DEFAULT_CHAIN=groq,openai,anthropic
DB_PATH=registry.db
DB_MAX_CONNECTIONS=32
SMTP_HOST=                      # leave blank locally; email silently no-ops
```

Production env vars and Stripe webhook config: see `docs/runbooks/deploy.md`.

**Deploy SSH key.** The prod deploy key lives at `./aztea_key.pem` in the repo root (gitignored via `*.pem`). `.env` points `DEPLOY_SSH_KEY` at this path so deploy scripts work from any shell context, including ones sandboxed out of `~/Downloads` by macOS TCC. Never commit the key; rotate via AWS console if it leaks.

---

## Public agent IDs

Source of truth: `server/builtin_agents/constants.py`. Curated public set (agents that demonstrate a unique platform primitive — subprocess isolation, live external data, headless runtimes) is in `CURATED_PUBLIC_BUILTIN_AGENT_IDS` — currently **11 agents** (the 10 from the 2026-05-26 platform-pivot cull — cve_lookup, dependency_auditor, dns_inspector, python_executor, multi_language_executor, live_sandbox, db_sandbox, browser_agent, lighthouse_auditor, accessibility_auditor — plus site_navigator, the agent-readable-web magnet added 2026-06-01). See `SUNSET_DEPRECATED_AGENT_IDS` for the 29 sunset entries and per-agent reasoning. Internal/hidden agents are in the same file. Always read constants directly; do not duplicate IDs anywhere else.
