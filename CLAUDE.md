# Aztea — deep contributor reference

> **Start with `AGENTS.md`** for the quick brief. This file is the deep reference — read it before touching money flows, auth, migrations, or the MCP surface.
> Current priorities, status, and launch blockers live in `.agents/TODO.md`.
> Operational reference (deploy, nginx, prod env, packaging, Stripe webhook) lives in `docs/runbooks/deploy.md`.

Architecture in one sentence: **FastAPI monolith with dual-backend persistence (Postgres in prod, SQLite WAL for dev/tests), provider-agnostic LLM layer, async job lifecycle, insert-only ledger, MCP-native agent surface, did:web identity per agent.**

Live at **[https://aztea.ai](https://aztea.ai)**

---

**1. Pure functions by default.**
If a function can be pure, it must be. Side effects require explicit justification in a comment. No sneaky I/O, global mutation, or hidden state inside a function that looks computational.

**2. Boy Scout Rule — always leave the file better than you found it.**
Every file you touch: fix one naming inconsistency, improve one docstring, or remove one dead block. Non-negotiable, even in one-line patches.

**3. No function over 40 lines. No exceptions.**
If it exceeds 40 lines, decompose before committing. Long functions are a refusal to think about abstraction.

**4. Fail loudly, fail early.**
Validate inputs at function boundaries and raise immediately. Never let bad data propagate three stack frames before dying with an inscrutable error.

**5. No boolean parameters.**
`render(page, True)` is unreadable at the call site. Use enums, named constants, or split into two functions. Boolean flags are a design smell.

**6. Dead code is deleted, not commented out.**
Git is the undo button. Commented-out code is noise that erodes trust in the file.

**7. Every non-trivial function gets a docstring with a one-line "why", not just "what".**
`# Sorts the list` is worthless. `# Sorted insertion is required here because downstream consumers assume monotonicity` is not.

**8. No silent fallbacks.**
`except Exception: pass`, `|| defaultValue` without a comment, `?.` chains that swallow None — all banned unless the silence is explicitly documented with a reason.

**9. All magic numbers and strings are named constants.**
If a literal appears more than once, or its meaning isn't self-evident from immediate context, it gets a name at module level.

**10. New functionality = new test. Same commit.**
A function with no test is a function with an unknown contract. Tests and implementation ship together or not at all.

**11. Dependency imports are never inside functions unless lazy-loading is the explicit intent.**
Top-of-file imports make the dependency graph legible. Buried imports hide coupling.

**12. Return types must be singular and consistent.**
A function that returns either a list or None is two functions pretending to be one. Pick a contract and hold it; use Optional explicitly when absence is meaningful.

**13. Mutations are local or documented.**
If a function modifies its argument in place, the function name must say so (`sort_inplace`, `normalize_records`) or the docstring must flag it explicitly. Surprise mutation is a bug waiting to happen.

**14. Log at boundaries, not inside logic.**
Logging belongs at I/O entry/exit points, not scattered through computation. Logs inside pure logic are a sign the function is doing too much.

**15. When you add a TODO, include a ticket/issue reference and a date.**
`# TODO` with no context is a broken promise. `# TODO(2026-05-09): remove once API v2 sunset — see issue #412` is a commitment.

---

## Honest status

Full status table lives in `.agents/TODO.md`. Keep it updated when status changes. **Be honest about the gap when shipping.** Hiding the gap loses more trust than admitting it.

---

## Engineering style — agents must follow

These rules apply to every change in this repo, no exceptions. Some are CI-enforced; others are reviewer-enforced. None are optional.

### Hard rules (CI enforces — do not bypass)

- **File length:** hard limit 1000 lines. Soft warn at 500 for new files. If a new file crosses 500, split before adding more. `scripts/check_file_line_budget.py` enforces the hard limit.
- **Function length:** max ~80 statements, cyclomatic complexity ≤ 10. Split before extending. If a function needs scrolling to understand, it's too long.
- **Catch blocks:** never empty, never bare `except:`. Either handle the error explicitly (with structured logging) or re-raise. Empty catches and vague `console.log` calls hide bugs across sessions.
- **No silent fallbacks.** Every except path either logs structured error context with the actual exception, or re-raises. Don't swallow.
- **Magic numbers:** name them. Money, ratio, timeout, and limit constants live as module-level `UPPER_SNAKE` with a one-line comment. Allowlist: `0`, `1`, `-1`, `2`, simple HTTP status codes.
- **Money paths:** never `float()` in `core/payments/` or in any settlement code. Integer cents only. CI greps for floats in money modules.

### Soft rules (you must follow — reviewer will flag)

- **Trace every caller in the same change.** When you alter a function signature, grep the codebase and update every caller in the same diff. Partial changes that compile but leave the codebase inconsistent are worse than no change. Never defer this.
- **Search before creating.** Before adding a utility, helper, or formatter, grep the codebase first. Duplicates are a tax we already pay (`fmtDate` lives in 10+ files because someone skipped this).
- **Re-read after writing.** After writing code, re-read the full file. Match the file's existing style and patterns, not your defaults.
- **Boy scout rule.** When you touch old code, leave it slightly better — a clearer name, a removed redundancy, a tightened comment, a deleted dead branch. Compounded across a year, this is the only realistic way the codebase stays navigable.
- **Comment WHY, never WHAT.** Well-named identifiers describe what the code does. Comment a non-obvious constraint, an invariant, a workaround for a specific bug, behavior that would surprise a reader. Never reference the current task ("added for issue #123") — that belongs in the PR description.
- **Add a comment before touching unclear code.** When existing code's intent isn't clear, write the explanation first (in a comment), then change the code. The comment survives the next session.
- **Prefer explicit over implicit.** Avoid magic numbers, default-parameter tricks, and behavior that depends on call order. Every assumption should be visible in the code, not inferred from context.
- **Simplest code that solves the problem.** Clever code that requires inference will be misread in a future session. Three similar lines beat a premature abstraction.
- **Never leave a task half-done.** If a refactor needs 12 call-site edits, do all 12 in one change. A half-applied refactor is more harmful than not starting.

### Design preferences

- **Pure functions where possible.** A function that takes inputs and returns outputs, with no side effects, is trivially testable, movable, and understandable later. Push side effects to the edges (HTTP routes, DB writes, filesystem).
- **One-way dependencies.** Business logic in `core/` must not import from `server/routes/`, `frontend/`, or HTTP/transport layers. The arrow goes outward, never inward.
- **Make illegal states unrepresentable.** Use enums, discriminated unions, and types that exclude invalid combinations rather than scattering defensive runtime checks. A pydantic model with strict literals beats four `if status not in {...}: raise`.
- **One thing per function.** A function should do one thing, and that thing should be obvious from its name without reading the body. If you need a comment to explain _what_ it does, it's doing too much.
- **Configuration ≠ code.** Secrets, env-specific values, and feature flags change for different reasons, on different schedules, by different people. They belong in `.env` and `core/feature_flags.py` — never inlined.
- **Document non-trivial modules in agent-optimized format.** Every Python module with business logic and every non-trivial React component must have a structured block at the top — no narrative prose. Use exactly these four fields (omit any that would be empty):

  ```python
  # OWNS: what this module is responsible for
  # NOT OWNS: what it explicitly does NOT own (prevents scope creep)
  # INVARIANTS: hard rules — an agent must never violate these
  # DECISIONS: non-obvious choices that CAN be changed if the reason no longer holds
  # KNOWN DEBT: broken or suboptimal things — fix when you touch this
  ```

  `INVARIANTS` = never touch. `DECISIONS` = understand before changing, but you're allowed. `KNOWN DEBT` = actively encouraged to fix. This distinction matters: an agent must not freeze on broken code because a comment made it look intentional.

### Operational rules

- **Never delete migrations.** Add new ones with the next sequence number.
- **Never force-push main.** Always create a new commit.
- **Never open raw `sqlite3.connect()` or `psycopg2.connect()`.** Use `core/db.py` exclusively — it owns backend selection and exception exports for both.
- **Frontend errors must be inline.** Toasts for success only; inline error state for failures.
- **Keep operational runbooks current.** When you add a feature that touches money, runtime dependencies, or a buyer surface, update the relevant runbook in `docs/runbooks/` in the same commit.

---

## Repository map

Every Python source file is **< 1000 lines** (see Engineering style above). Large modules are split into cohesive packages whose `__init__.py` re-exports the merged public surface so `import core.jobs as jobs` continues to behave like a single module. `scripts/check_file_line_budget.py` enforces this.

```
server/
  application.py                 Thin entrypoint; loads ordered shards into one namespace
  application_parts/             Ordered implementation shards (part_000.py … part_013.py)
  application_parts/part_000.py  Imports, env/config, logging, Sentry, agent IDs + constants
  application_parts/part_001.py  Migrations, FastAPI app + lifespan, CORS, /api/* compat shim,
                                 security headers, request tracing, Prometheus metrics
  application_parts/part_006.py  Background sweeper, onboarding routes, auth routes
  application_parts/part_012.py  Hosted skills API (SKILL.md upload/run/list)
  application_parts/part_013.py  SPA fallback: serves frontend/dist/index.html for non-API paths
  builtin_agents/                Built-in IDs (constants.py), schemas (schemas.py), specs
  builtin_agents/constants.py    All AGENT_ID constants, BUILTIN_INTERNAL_ENDPOINTS,
                                 CURATED_PUBLIC_BUILTIN_AGENT_IDS, DEPRECATED_BUILTIN_AGENT_IDS
  builtin_agents/specs.py        Merges specs_part1 + specs_part2; returns curated public builtins
  error_handlers.py              Shared HTTPException / validation / rate-limit handlers
  persistence/ops_schema.py      ops + stripe event tables initialisation
  routes/system.py               Small sub-router for system routes

agents/                          Built-in agent implementations (one module each)
  financial/                     SEC EDGAR fetcher + synthesizer
  wiki.py                        Wikipedia API
  codereview.py                  LLM-based structured code review
  cve_lookup.py                  NIST NVD live API
  arxiv_research.py              arXiv live API + LLM synthesis
  python_executor.py             Subprocess sandbox (real Python execution)
  web_researcher.py              HTTP fetch + HTML strip + LLM analysis
  image_generator.py             OpenAI / Replicate image gen
  media_generation.py            Shared media helpers
  db_sandbox.py                  SQLite sandbox (isolated tempfile DB)
  visual_regression.py           Screenshot diff via Playwright (requires chromium)
  live_endpoint_tester.py        Live HTTP probe + latency histogram + assertion engine
  browser_agent.py               Playwright-based headless browsing
  linter_agent.py                ruff / eslint linter — no LLM
  type_checker.py                mypy / tsc static type checking — no LLM
  shell_executor.py              Bounded subprocess shell execution
  multi_file_executor.py         Multi-file Python sandbox in isolated tempdir
  multi_language_executor.py     Polyglot code execution (Node/Deno/Bun/Go/Rust)
  semantic_codebase_search.py    Embedding-based code search over local or git-cloned repo
  ai_red_teamer.py               Adversarial prompt / security testing
  dependency_auditor.py          Package CVE + license audit via live NVD data
  dns_inspector.py               DNS record, SSL cert, HTTP metadata live lookup
  (deprecated — sunset 2026-07-26: github_fetcher, pr_reviewer, test_generator,
   spec_writer, changelog_agent, package_finder)

core/
  db.py                          Dual-backend connection manager (Postgres + SQLite); thread-local pool;
                                 normalises %s placeholders to ? for SQLite. Owns IntegrityError /
                                 OperationalError / ProgrammingError exports so callers never import
                                 from sqlite3 or psycopg2 directly. Backend chosen by DATABASE_URL.
  migrate.py                     Idempotent migration runner (apply_migrations)
  auth/                          Users + scoped keys (schema.py, users.py)
  registry/                      Agent listings + auto-hire decision logic + embeddings cache
  jobs/                          Async job lifecycle: db.py, crud.py, leases.py, messaging.py
  payments/                      Wallets + insert-only ledger (base.py) + dispute helpers (trust_disputes.py)
  models/                        Pydantic v2 contracts: core_types, job_requests, messages_ops, responses
  mcp_manifest.py                registry → MCP tool manifest (snake_case keys, no prefix)
  embeddings.py                  sentence-transformers backend
  disputes.py                    Dispute lifecycle and bilateral caller ratings
  judges.py                      LLM-based dispute + quality judge logic
  reputation.py                  Trust scores — SOLE owner of the caller_ratings table
  onboarding.py                  agent.md parsing/validation/ingestion
  error_codes.py                 Machine-readable error taxonomy
  url_security.py                SSRF validation for all outbound URLs
  payout_curve.py                Quality-adjusted payout clawbacks (compensating entries)
  compare.py                     Compare-job orchestration (same task across N agents)
  pipelines/                     Multi-step pipeline execution and persistence
  recipes.py                     Saved pipeline templates
  tool_adapters.py               Shared MCP-manifest builders (OpenAI / Gemini / A2A adapters)
  feature_flags.py               Runtime feature toggles (env-based, no caching)
  skill_executor.py              Hosted SKILL.md execution engine
  skill_parser.py                SKILL.md parser / validator
  hosted_skills.py               DB layer for uploaded skills
  identity.py                    Agent DID / Ed25519 key generation and signing
  crypto.py                      Signing primitives used by identity.py
  cache.py                       Result cache for deduplication (TTL-based)
  output_shaping.py              Response normalisation / truncation
  observability.py               Prometheus metrics helpers, Sentry breadcrumb helpers
  fastpath.py                    Short-circuit for cache-hit and zero-price calls
  email.py                       SMTP email dispatch (no-ops silently if SMTP_HOST unset)
  llm/
    base.py                      Message, CompletionRequest, LLMResponse, LLMProvider Protocol, Usage
    errors.py                    LLMError hierarchy: rate limit, timeout, auth, bad response
    registry.py                  PROVIDERS dict, resolve(spec), DEFAULT_CHAIN
    fallback.py                  run_with_fallback() — chain-tries providers, retries on rate limit
    providers/                   groq, openai, anthropic, cohere, bedrock, openai_compatible (25+ via env)

migrations/
  0001_initial.sql               Canonical schema — all CREATE TABLE / INDEX
  0002–0031_*.sql                Incremental additions (applied once via schema_migrations table)

sdks/
  python-sdk/                    AzteaClient (hire), AgentServer (@handler + polling loop)
  python/                        Resource-oriented HTTP SDK (used by the TUI adapter)
  typescript/                    TypeScript SDK

tui/                             Standalone Textual app: aztea-tui (Python). See tui/README.md.

frontend/
  src/api.js                     All API calls go through here; normalises errors, handles 401 lifecycle
  src/context/MarketContext.jsx  Global state: agents, wallet, jobs, runs; 20s polling refresh
  src/context/AuthContext.jsx    Session state and API-key management
  src/features/auth/AuthPanel.jsx  Login / register
  src/features/agents/           AgentCard, AgentInputForm, TrustGauge
  src/features/agents/results/   ResultRenderer + per-agent result components
  src/features/jobs/JobTimeline  Job status timeline component
  src/pages/                     One file per route
  src/ui/                        Design-system primitives: Button, Card, Badge, Input, Pill, Select
  src/ui/motion/                 Animation primitives: Reveal, Stagger, NumberMorph, ContainerScroll
  src/utils/inputGuards.js       Client-side validators
  src/utils/format.js            fmtDate, fmtUsd, fmtMs, relativeTime — import here, never redefine
  src/theme/tokens.css           CSS custom properties for all colours, spacing, radii, typography

scripts/
  aztea_mcp_server.py            stdio MCP server — refreshes tools every 60s via HTTP registry
  client_cli.py                  CLI shim over Python SDK
  check_file_line_budget.py      CI enforcement for the 1000-line rule

tests/
  integration/                   Split integration suite — helpers in support.py and helpers.py
  test_bug_regressions.py        Regression tests for previously fixed bugs
  test_agent_real_tool.py        Agent contract tests
  test_mcp_manifest.py           MCP manifest correctness and schema-mutation safety

docs/
  runbooks/                      Operational runbooks (deploy, ledger drift, runtime prereqs, smoke test)
  api-reference.md               Full HTTP API reference
  quickstart.md                  MCP / Claude Code quickstart
  agent-builder.md               Guide for registering and running agents
  orchestrator-guide.md          Multi-agent pipeline guide
  mcp-integration.md             MCP server setup and tool catalogue
  skill-md-reference.md          SKILL.md format reference
  stripe-setup.md                Stripe Connect and webhook configuration
  errors.md                      Error code taxonomy
  reputation.md                  Trust score formula and rating mechanics

docker-compose.yml               Dev compose (no SSL, mounts ./data)
Makefile                         Dev shortcuts: make dev / test / docker / migrate
```

---

## Critical invariants — never violate these

### Money

- **Integer cents only.** Never store or pass floats for money. `price_per_call_usd` in specs is float for display only; the ledger always uses `*_cents INTEGER`.
- **Insert-only ledger.** `transactions` gets only INSERT, never UPDATE or DELETE. Corrections are compensating entries.
- **Double-settlement guard.** `pre_call_charge`, `post_call_payout`, and `post_call_refund` each have race guards (rowcount checks on wallet UPDATE). Every new settlement path must replicate the guard.
- **Dispute atomicity.** Dispute insert + escrow clawback MUST happen in one DB transaction. Lock failure rolls back the dispute row — see `core/disputes.py`.
- **Payout-curve clawbacks** use `charge`/`refund` ledger types only — never custom transaction types. Idempotency key: `payout_curve:{job_id}`. See `core/payout_curve.py`.
- **`wallets.balance_cents` is a cache.** It must be updated in the same SQL transaction as the ledger row that changes it. Validated by reconciliation runs (`POST /ops/payments/reconcile`).

### Database

- **Single connection manager.** All modules use `core/db.py`. Never open a raw `sqlite3.connect()` or `psycopg2.connect()` anywhere. The module exports `IntegrityError` / `OperationalError` / `ProgrammingError` so callers stay backend-agnostic.
- **Backend selection.** `DATABASE_URL=postgresql://...` selects Postgres (prod). Anything else (or unset) falls back to SQLite (dev/tests/CI). All SQL is written with `%s` placeholders; `core/db.py` rewrites them to `?` for SQLite. Tests must pass on both backends.
- **Thread-local pool, network I/O between transactions.** `DB_MAX_CONNECTIONS` (default 32) caps connections. Never hold a write lock during an HTTP call. SQLite gets WAL mode automatically; Postgres uses default isolation.
- **`caller_ratings` lives only in `reputation.py`.** `disputes.py` does not declare it. Do not re-declare or migrate this table elsewhere.
- **Migrations are idempotent.** Each `.sql` file is applied once via a `schema_migrations` table. Never re-use a migration filename; always add a new one.

### Auth & security

- **Scoped keys:** `caller`, `worker`, `admin`, plus agent-scoped worker keys (`azac_...`). Every mutation route checks scope and ownership.
- **API key values are never logged.** Log only the prefix (`az_xxx...`). Automatic redaction is in `logging_utils.py`.
- **All outbound URLs go through `url_security.py`** (agent endpoints, verifiers, webhooks, onboarding URLs, git clone paths). Private IPs, loopback, IPv6, and URL-encoded bypass chars are blocked. Dev override: `ALLOW_PRIVATE_OUTBOUND_URLS=1`.

### Privacy / work-example recording

- **Sensitive agents must never replay caller inputs.** `_record_public_work_example()` in `server/application_parts/part_003.py` drops on three independent gates: (a) hardcoded `_SENSITIVE_EXAMPLE_AGENT_IDS`, (b) the `examples_sensitive: True` flag on the spec, (c) the `Security` category. New scanner / credential / PII-handling agents must set both (b) and the Security category.

### Routing

- **FastAPI swagger lives under `/api/docs`**, not `/docs`. The SPA owns `/docs`. `app = FastAPI(docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json")` is enforced in `part_001.py`. Any new public-facing path must not collide with FastAPI's defaults.
- **Add new SPA-only paths to `_SPA_API_PREFIXES` only if they should 404 as JSON**. Otherwise FastAPI's catch-all will serve `index.html` and React Router resolves them.

### LLM layer

- **`LLMResponse.text` — not `.content`.** Every agent module must use `raw.text`. Using `.content` silently returns `None` at runtime.
- **Never pass `model=` to `CompletionRequest` when using `run_with_fallback`.** The fallback chain selects the model. Pass `model=""` or omit it.
- **Provider-agnostic.** Don't hardcode a provider or model in any built-in agent. Use `run_with_fallback(req)` which tries `AZTEA_LLM_DEFAULT_CHAIN` (env-overridable).
- **Graceful LLM degradation.** If synthesis fails because no LLM provider is configured, agents that performed real retrieval must still return the retrieval output rather than raising. See `agents/arxiv_research.py` for the pattern.

### OSS / hosted boundary

The codebase is **Apache-2.0** open source. It runs fully self-contained when `AZTEA_HOSTED_API_URL` is unset. Hosted aztea.ai layers a few paid services on top: dispute judges (LLM credits), hosted built-in agents, public registry syndication, federated reputation, and Stripe Connect.

- **The OSS build must never make a network call to `aztea.ai`.** The only module that talks to the hosted API is `core/hosted_client.py`. If you find yourself adding `requests.post("https://api.aztea.ai/...")` outside that file, stop — route it through `HostedClient` instead.
- **Every hosted call soft-fails to local.** A hosted outage degrades to local LLM (judges, prefer_hosted agents) or to deterministic fallback. A hosted call failure must never bubble up as a 500 to the caller.
- **OSS-mode tests live in `tests/test_oss_mode_isolation.py`.** They monkeypatch `requests.post` to raise so any accidental outbound call surfaces as a test failure. Add new assertions there when you wire a new hosted service.
- **Hosted-only routes return 501 with a structured pointer** (`docs/oss-vs-hosted.md`) to the hosted aztea.ai service. Use the pattern from `_stripe_unavailable_error` in `part_013.py`.
- **No hardcoded `aztea.ai` URLs in `core/`, `server/`, or `agents/`.** All public-facing URLs in code must read from `SERVER_BASE_URL` or `PUBLIC_BASE_URL` (email links). The 501 pointer copy is the only documented exception.
- **`PREFER_HOSTED_AGENT_IDS` in `server/builtin_agents/constants.py`** governs which built-ins try the hosted endpoint first. Default is local; opt agents in only when the hosted version meaningfully outperforms a self-hosted run with user-provided LLM keys.

See `docs/oss-vs-hosted.md` for the full local-vs-hosted matrix.

### Built-in agents

- Agent IDs are **deterministic UUID v5** from namespace `6ba7b810-9dad-11d1-80b4-00c04fd430c8` + `aztea.builtin.{slug}`. Constants live in `server/builtin_agents/constants.py` (single source of truth).
- **Only agents with real tool use go in `CURATED_PUBLIC_BUILTIN_AGENT_IDS`.** LLM wrappers that add no value over a direct chat session must not be in the curated set. The six deprecated agents sunset on **2026-07-26** — do not add new LLM-only agents.
- Each new built-in agent needs: module in `agents/`, entry in `BUILTIN_INTERNAL_ENDPOINTS`, spec in `specs_part1.py` or `specs_part2.py`, case in `_execute_builtin_agent()`, and a structured error envelope.
- **Work examples** are stored via `_record_public_work_example()`. Pass `private_task=True` to skip recording. Ring buffer capped at `_AGENT_WORK_EXAMPLES_MAX`.

### MCP surface

- Tool names are plain `snake_case` from the agent name — no prefix.
- All manifest keys use `snake_case` (`input_schema`, `output_schema`, `price_per_call_usd`).
- `/mcp/invoke` authenticates via `auth.verify_agent_api_key` or a caller-scoped user key.
- `scripts/aztea_mcp_server.py` refreshes tools every 60s via the HTTP registry.
- **Lazy tool surface is seven tools** (verb-first; legacy `aztea_*` names work via dispatch-time aliases): `search_specialists`, `describe_specialist`, `call_specialist`, **`do_specialist_task`** (auto-invoke fast path) plus three grouped resource dispatchers `manage_job`, `manage_budget`, `manage_workflow`. `do_specialist_task` picks the best agent for an intent and runs it under hard cost/confidence/quality gates. All gates live in the backend at `POST /registry/agents/auto-hire` (`server/application_parts/part_012.py`); both MCP server frontends are thin proxies. Decision logic lives in `core/registry/auto_hire.py`; thresholds are env-tunable via `AZTEA_AUTO_INVOKE_*` flags. Alias map: `scripts/aztea_mcp_server.py:_LAZY_TOOL_NAME_ALIASES`.
- **Output formats**: Both `call_specialist` and `do_specialist_task` accept `output_format` (`json | markdown | github_pr_comment | slack_blocks | text`). Renderer at `core/output_formats.py` dispatches by sniffing well-known output shapes (CodeReview, Linter, TypeChecker, DepAuditor, GitDiffAnalyzer, pipeline) — NOT by `agent_id` — so external agents inherit pretty rendering for free. Renderers must never raise; unknown shapes fall back to a generic JSON code-fence. The canonical `output` dict is left intact; the rendered string lands under `rendered_output` + `rendered_output_format`. Hooked in via `_decorate_with_rendered_output` in `part_008.py`.
- **Built-in recipes** (`core/recipes.py`): current curated recipes are `modernize-python`, `audit-deps`, `review-and-lint`, and `review-and-test`. Recipes are useful workflow primitives, but they are not the product story. Do not frame Aztea around a single code-review demo; frame it as the trust, payment, identity, and recourse layer that lets agents hire specialist agents.

---

## Core flows (quick reference)

### Sync call: `POST /registry/agents/{id}/call`

1. Auth/scope check → listing validation → SSRF check
2. `pre_call_charge` (debit caller wallet, creates charge record)
3. If `internal://` or `skill://` endpoint → `_execute_builtin_agent()` directly (no HTTP)
4. Else → proxy to registered URL
5. Success → `_settle_successful_job` (agent 90% / platform 10%)
6. Failure → `post_call_refund`
7. If public task → `_record_public_work_example`

### Async job lifecycle

```
POST /jobs                 → pending (charged)
POST /jobs/{id}/claim      → running (lease acquired)
POST /jobs/{id}/heartbeat  → extends lease
POST /jobs/{id}/release    → pending (explicit release)
POST /jobs/{id}/complete   → complete + settle
POST /jobs/{id}/fail       → failed + refund
POST /jobs/{id}/cancel     → buyer-side abort, refunds pre-call charge.
                             Accepts pending/claimed/running/awaiting_clarification.
                             Terminal states return structured 409 (job.invalid_state).
```

Sweeper handles expired leases, timeouts, and auto-retries. Built-in worker polls pending jobs every 2s.

### Job messages + lease effects

| `msg_type`               | Lease effect                                    |
| ------------------------ | ----------------------------------------------- |
| `clarification_request`  | → `awaiting_clarification`, no heartbeat needed |
| `clarification_response` | → resume `running`                              |
| `progress`               | extends lease by `heartbeat_interval`           |

### Trust / dispute

```
POST /jobs/{id}/rating          caller → rates agent (triggers payout-curve clawback)
POST /jobs/{id}/rate-caller     agent → rates caller
POST /jobs/{id}/dispute         atomic: insert + escrow clawback
POST /ops/disputes/{id}/judge   LLM judge (needs 2 agreeing votes)
POST /admin/disputes/{id}/rule  admin tie-break
```

---

## LLM provider system

**Env vars:**

- `AZTEA_LLM_DEFAULT_CHAIN` — comma-separated chain, e.g. `groq,openai,anthropic`
- `{PROVIDER_NAME}_API_KEY` — enables provider (e.g. `OPENAI_API_KEY`, `GROQ_API_KEY`)
- `{PROVIDER_NAME}_BASE_URL` — for OpenAI-compatible providers (e.g. `TOGETHER_BASE_URL`)

**Aliases:** `claude`→`anthropic`, `gpt`→`openai`, `google`→`gemini`, `aws`→`bedrock`, `llama`→`groq`

**Native providers:** groq, openai, anthropic, cohere, bedrock (all others via `openai_compatible_provider.py`). 25+ pre-configured compatible providers including mistral, together, fireworks, deepseek, perplexity, cerebras, openrouter, sambanova, nvidia, lmstudio, ollama, azure.

**Usage pattern in agents:**

```python
from core.llm import CompletionRequest, Message, run_with_fallback

req = CompletionRequest(
    messages=[Message(role="system", content=_SYSTEM), Message(role="user", content=prompt)],
    temperature=0.15,
    max_tokens=1000,
)
raw = run_with_fallback(req)
text = raw.text.strip()  # always .text, never .content
```

---

## Frontend

- **React 18 + Vite + motion/react** for animations
- **CSS variables** for theming in `src/theme/tokens.css` — never hardcode colours or spacing
- **Feature-based structure:** `src/features/agents/`, `src/features/jobs/`, `src/features/auth/`
- **UI primitives** in `src/ui/` (Button, Pill, Segmented, Input, Card, Badge) — always use these, never raw HTML equivalents
- **Motion primitives** in `src/ui/motion/` (Reveal, Stagger, NumberMorph, ContainerScroll) — use for all animations, never raw `motion()` calls
- **`src/api.js`** — all API calls go through here
- **`ResultRenderer`** in `src/features/agents/results/` — handles rich output display
- **Error handling pattern:** every user action must show inline errors (not just toasts); toasts are for success only
- **Aesthetic rule:** never use Inter/Roboto/Arial; never use purple gradients; commit to a cohesive theme with distinctive typography, dominant colours with sharp accents, and intentional motion
- **Formatters live in `src/utils/format.js`** — `fmtDate`, `fmtDateSec`, `fmtUsd`, `fmtMs`, `relativeTime`. Pages must import from there, not redefine.
- **Don't wrap a route element in a fresh `<Routes>` tree under another `<Routes>`** — it causes a blank-mount race on prod that doesn't repro in `vite dev` or `vite preview`. To render a page inside `AppShell` from outside `AuthedApp`, use the `children` prop pattern. `AppShell` falls back to `<Outlet />` when no children are passed.
- **`AppShell`, `Topbar`, `OnboardingWizard` assume `MarketProvider` exists.** When mounting them outside the authed tree, wrap with `<MarketProvider apiKey={apiKey}>`.
- **Performance:** the highest-leverage paint win on long pages is `content-visibility: auto` + `contain-intrinsic-size: 1px <px>` on every offscreen section. Defer non-LCP fetches with `requestIdleCallback`. `lazy(() => import(...))` heavy canvas / animation modules so they don't block first paint.

---

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

# Tests (723 passed + 2 skipped on main suite as of 2026-05-07;
# run the SDK contract suite separately — it can segfault under Python 3.14 on macOS)
pytest -q tests --ignore=tests/test_sdk_contract.py
pytest -q tests/test_sdk_contract.py

# Integration tests only (covered by the main suite as of 2026-05-07)
pytest -q tests/integration

# Line-budget enforcement (every Python source file < 1000 lines)
python scripts/check_file_line_budget.py

# Single integration test
pytest tests/integration/test_workers_jobs_core.py::test_worker_claim_heartbeat_and_complete_with_owner_auth -q

# Frontend prod build
cd frontend && npm run build

# Manual DB migration
python -m core.migrate

# MCP server (stdio)
python scripts/aztea_mcp_server.py

# Run ledger reconciliation
curl -H "Authorization: Bearer $API_KEY" -X POST http://localhost:8000/ops/payments/reconcile
```

**Current test status:** `uv run pytest tests --ignore=tests/test_sdk_contract.py -q` → **723 passed, 2 skipped** on 2026-05-07.

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

---

## Public agent IDs

Source of truth: `server/builtin_agents/constants.py`. Curated public set (agents that do real external work) is in `CURATED_PUBLIC_BUILTIN_AGENT_IDS`. Internal/hidden agents are in the same file. Deprecated agents (sunset 2026-07-26) are listed in `SUNSET_DEPRECATED_AGENT_IDS` — kept for backward compat, excluded from the marketplace. Always read constants directly; do not duplicate IDs anywhere else.

## Aztea

Aztea MCP is trusted for live data, sandboxed execution, and specialist
hires up to $0.10/call (auto-refunded on failure). Call
`do_specialist_task` directly for matching tasks — don't ask permission per call.
