# Aztea — contributor guide

## What this is

Aztea is an AI agent labor marketplace: callers hire agents by the task, workers earn revenue, and the platform handles billing, escrow, settlement, trust, and dispute resolution transparently. Think Stripe + Upwork + Dun & Bradstreet — but for AI agents.

Architecture in one sentence: **FastAPI monolith on SQLite WAL, provider-agnostic LLM layer, async job lifecycle, insert-only ledger, MCP-native agent surface.**

Live at **[https://aztea.ai](https://aztea.ai)**

---

## Non-negotiable engineering rules

These apply to every change, no exceptions:

- **Never delete migrations.** Add new ones with the next sequence number.
- **Never force-push main.** Always create a new commit.
- **Never open raw `sqlite3.connect()`.** Use `core/db.py` exclusively.
- **Never store floats in the ledger.** Integer cents only.
- **Frontend errors must be inline.** Toasts for success only; inline error state for failures.
- **Comment and document every non-trivial module.** Every Python module that contains business logic must have a module-level docstring explaining what it owns, what it does not own, and the key invariants a contributor must not violate. Functions with non-obvious behaviour, complex math, or safety-critical paths must have a docstring. "I can read the code" is not a substitute. Undocumented invariants rot — they become the bugs in the next session. The same rule applies to new React components: a one-line comment above the component explaining what it renders and any non-obvious state is mandatory.
- **Keep operational runbooks current.** When you add a feature that touches money, runtime dependencies, or a buyer surface, update the relevant runbook in `docs/runbooks/` in the same commit.

---

## Repository map

Every Python source file is kept **< 1000 lines**. Large modules are split into cohesive packages whose `__init__.py` re-exports the merged public surface so `import core.jobs as jobs` (and similar) continue to behave like a single module. `scripts/check_file_line_budget.py` enforces this rule.

```
server/
  application.py                 Thin entrypoint; loads ordered shards into one namespace
  application_parts/             Ordered implementation shards (part_000.py … part_013.py)
  application_parts/part_000.py  Imports, env/config, logging, Sentry, agent IDs + constants
  application_parts/part_001.py  Migrations, FastAPI app + lifespan, CORS, /api/* compat shim,
                                 security headers, request tracing, Prometheus metrics
  application_parts/part_006.py  Background sweeper, onboarding routes, auth routes (first to register routes)
  application_parts/part_012.py  Hosted skills API (SKILL.md upload/run/list)
  application_parts/part_013.py  SPA fallback: serves frontend/dist/index.html for non-API paths
  builtin_agents/                Built-in IDs (constants.py), schemas (schemas.py), and registration specs
  builtin_agents/constants.py    All AGENT_ID constants, BUILTIN_INTERNAL_ENDPOINTS,
                                 CURATED_PUBLIC_BUILTIN_AGENT_IDS, DEPRECATED_BUILTIN_AGENT_IDS
  builtin_agents/specs.py        Merges specs_part1 + specs_part2; returns only curated public builtins
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
  media_generation.py            Shared media helpers (used by image/video agents)
  db_sandbox.py                  SQLite sandbox (real query execution, isolated tempfile DB)
  visual_regression.py           Screenshot diff via Playwright (requires chromium)
  live_endpoint_tester.py        Live HTTP probe + latency histogram + assertion engine
  browser_agent.py               Playwright-based headless browsing (requires chromium)
  linter_agent.py                ruff (Python) / eslint (JS/TS) linter — no LLM
  type_checker.py                mypy / tsc static type checking — no LLM
  shell_executor.py              Bounded subprocess shell execution
  multi_file_executor.py         Multi-file Python sandbox in isolated tempdir
  multi_language_executor.py     Polyglot code execution (Node/Deno/Bun/Go/Rust)
  semantic_codebase_search.py    Embedding-based code search over local or git-cloned repo
  ai_red_teamer.py               Adversarial prompt / security testing against registered agents
  dependency_auditor.py          Package CVE + license audit via live NVD data
  dns_inspector.py               DNS record, SSL cert, and HTTP metadata live lookup
  (deprecated — sunset 2026-07-26: github_fetcher, pr_reviewer, test_generator,
   spec_writer, changelog_agent, package_finder — LLM-only wrappers kept for
   backward compat but excluded from the public marketplace)

core/
  db.py                          SQLite connection manager — WAL, thread-local pool, PRAGMAs
  migrate.py                     Idempotent migration runner (apply_migrations)
  auth/                          Users + scoped keys (schema.py, users.py) merged into core.auth
  registry/                      Agent listings (core_schema.py, agents_ops.py) + embeddings cache
  jobs/                          Async job lifecycle: db.py, crud.py, leases.py, messaging.py
  payments/                      Wallets + insert-only ledger (base.py) + dispute helpers (trust_disputes.py)
  models/                        Pydantic v2 contracts: core_types, job_requests, messages_ops, responses
  mcp_manifest.py                registry → MCP tool manifest (snake_case keys, no prefix)
  embeddings.py                  sentence-transformers backend
  disputes.py                    Dispute lifecycle and bilateral caller ratings (atomic insert + escrow clawback)
  judges.py                      LLM-based dispute + quality judge logic
  reputation.py                  Trust scores — SOLE owner of the caller_ratings table
  onboarding.py                  agent.md parsing/validation/ingestion
  error_codes.py                 Machine-readable error taxonomy
  url_security.py                SSRF validation for all outbound URLs
  payout_curve.py                Quality-adjusted payout clawbacks (agent→caller compensating entries)
  compare.py                     Compare-job orchestration (same task across N agents side-by-side)
  pipelines/                     Multi-step pipeline execution and persistence
  recipes.py                     Saved pipeline templates
  tool_adapters.py               Shared MCP-manifest builders for OpenAI-tools / Gemini-tools / A2A adapters
  feature_flags.py               Runtime feature toggles (env-based, no caching — safe to reload via SIGHUP)
  skill_executor.py              Hosted SKILL.md execution engine (routes skill:// endpoint calls)
  skill_parser.py                SKILL.md parser / validator
  hosted_skills.py               DB layer for uploaded skills
  identity.py                    Agent DID / Ed25519 key generation and signing
  crypto.py                      Signing primitives used by identity.py
  cache.py                       Result cache for deduplication (TTL-based, keyed by agent + payload hash)
  output_shaping.py              Response normalisation / truncation before serialisation
  observability.py               Prometheus metrics helpers, Sentry breadcrumb helpers
  fastpath.py                    Short-circuit fast-path for cache-hit and zero-price calls
  email.py                       SMTP email dispatch (8 templates; no-ops silently if SMTP_HOST unset)
  compare.py                     Compare-job routing and result aggregation
  llm/
    base.py                      Message, CompletionRequest, LLMResponse, LLMProvider Protocol, Usage
    errors.py                    LLMError hierarchy: rate limit, timeout, auth, bad response
    registry.py                  PROVIDERS dict, resolve(spec), DEFAULT_CHAIN, list_providers()
    fallback.py                  run_with_fallback() — chain-tries providers, skips unavailable, retries on rate limit
    providers/                   groq, openai, anthropic, cohere, bedrock, openai_compatible (25+ via env)

migrations/
  0001_initial.sql               Canonical schema — all CREATE TABLE / INDEX
  0002–0029_*.sql                Incremental additions (applied once on startup via schema_migrations table)

sdks/
  python-sdk/                    AzteaClient (hire), AgentServer (@handler + polling loop)
  python/                        Resource-oriented HTTP SDK (aztea package; used by the TUI adapter)
  typescript/                    TypeScript SDK

tui/
  pyproject.toml                 Standalone package aztea-tui (Textual); console entry aztea-tui
  README.md                      Install, key bindings, architecture (screens, views, AzteaAPI adapter)
  aztea_tui/app.py               Textual AzteaApp: login vs main from config.load_config()
  aztea_tui/api.py               AzteaAPI — async façade over blocking AzteaClient
  aztea_tui/screens/             LoginScreen, MainScreen (sidebar + ContentSwitcher)
  aztea_tui/views/               Agents, jobs, wallet, my agents
  aztea_tui/widgets/             Header bar, hire modal, live job polling

frontend/
  src/api.js                     All API calls go through here; normalises errors, handles 401 lifecycle
  src/context/MarketContext.jsx  Global state: agents, wallet, jobs, runs; 20s polling refresh
  src/context/AuthContext.jsx    Session state and API-key management
  src/features/auth/AuthPanel.jsx  Login / register with username + password rules enforced before request
  src/features/agents/           AgentCard, AgentInputForm, TrustGauge
  src/features/agents/results/   ResultRenderer + per-agent result components (CodeReviewResult, etc.)
  src/features/jobs/JobTimeline  Job status timeline component
  src/pages/                     One file per route (AgentDetailPage, JobDetailPage, WalletPage, etc.)
  src/ui/                        Design-system primitives: Button, Card, Badge, Input, Pill, Select, etc.
  src/ui/motion/                 Animation primitives: Reveal, Stagger, NumberMorph, ContainerScroll, etc.
  src/utils/inputGuards.js       Client-side validators: public HTTPS URLs, price ceilings, invoke payload
  src/theme/tokens.css           CSS custom properties for all colours, spacing, radii, and typography

scripts/
  aztea_mcp_server.py            stdio MCP server — refreshes tools every 60s via HTTP registry
  client_cli.py                  CLI shim over Python SDK
  check_file_line_budget.py      CI enforcement for the 1000-line rule
  split_python_by_ast.py         Helper that shards oversized modules on top-level AST boundaries

tests/
  integration/                   Split integration suite — helpers in support.py and helpers.py
  test_bug_regressions.py        Regression tests for previously fixed bugs (money paths, agent contracts)
  test_agent_real_tool.py        Agent contract tests: structured errors, graceful no-LLM fallback
  test_mcp_manifest.py           MCP manifest correctness and schema-mutation safety
  …                              Unit tests for jobs, payments, registry, auth, LLM, SDK

docs/
  runbooks/                      Operational runbooks — ledger drift, runtime prereqs, smoke tests
  api-reference.md               Full HTTP API reference
  quickstart.md                  MCP / Claude Code quickstart
  agent-builder.md               Guide for registering and running agents
  orchestrator-guide.md          Multi-agent pipeline guide
  mcp-integration.md             MCP server setup and tool catalogue
  skill-md-reference.md          SKILL.md format reference for hosted skills
  stripe-setup.md                Stripe Connect and webhook configuration
  errors.md                      Error code taxonomy and client handling guide
  reputation.md                  Trust score formula and rating mechanics

docker-compose.yml               Dev compose (no SSL, mounts ./data)
docker-compose.prod.yml          Prod compose (nginx + API, named volume for DB)
nginx.prod.conf                  nginx reverse proxy — /api/* → FastAPI, /* → React SPA
Makefile                         Dev shortcuts: make dev / test / docker / migrate
```

---

## Production deployment

### Cloudflare + EC2 (typical)

- **DNS:** Point the hostname to the EC2 public IP. With Cloudflare proxy (orange cloud) on, set SSL/TLS to **Full (strict)** — this requires a valid cert on the origin (certbot + nginx).
- **Client IP:** Terminate at nginx, forward `X-Forwarded-For` / `X-Real-IP`, configure `TRUSTED_PROXY_IPS` so `slowapi` and admin checks see the real client IP.
- **Env URLs:** `SERVER_BASE_URL`, `FRONTEND_BASE_URL`, and `CORS_ALLOW_ORIGINS` must use the public `https://` hostname, not the raw EC2 IP.

### Infrastructure

- **Server:** AWS EC2 Ubuntu — `/home/aztea/app`
- **Stack:** systemd service (`aztea.service`) running uvicorn directly — no Docker in production
- **Process:** `/home/aztea/app/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000 --workers 1`
- **Database:** SQLite WAL at the path set in `.env` (`DB_PATH`), on the host filesystem
- **Reverse proxy:** nginx on ports 80/443. `/api/*` → uvicorn on `127.0.0.1:8000`; everything else served from `frontend/dist/` with SPA fallback. The backend also handles an un-stripped `/api/` prefix and serves `frontend/dist/index.html` as a fallback, so the site stays functional even if nginx and FastAPI disagree on which layer owns static assets.
- **SSL:** Managed by certbot on the host; nginx handles termination.

### Deploying a new version

SSH into the server, then:

```bash
cd /home/aztea/app

# 1. Pull as the service user — NEVER sudo git pull (makes files root-owned,
#    breaks the systemd unit that runs as `aztea`).
sudo -u aztea git fetch origin main
sudo -u aztea git reset --hard origin/main

# 2. Rebuild the React frontend
cd frontend && npm ci && npm run build && cd ..

# 3. Restart the API (migrations run automatically on startup)
sudo systemctl kill -s SIGKILL aztea   # force-kill if stuck in shutdown
sudo systemctl start aztea

# 4. Verify
sudo systemctl status aztea
sudo journalctl -u aztea -n 50
```

**If the service stops cleanly** (not stuck), `restart` is fine:

```bash
sudo systemctl restart aztea
```

Migrations run automatically on startup via `core/migrate.py` — no manual step needed.

### Recommended nginx config

```nginx
server {
    listen 443 ssl http2;
    server_name aztea.ai www.aztea.ai;

    root /home/aztea/app/frontend/dist;
    index index.html;

    # Hashed Vite assets — long cache
    location ~* ^/assets/.*\.(js|css|woff2?|ttf|eot|svg|png|jpg|jpeg|gif|webp|ico|map)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
        try_files $uri =404;
    }

    # API + server routes → uvicorn (strip the /api prefix)
    location ~ ^/(api|auth|admin|agents|jobs|registry|wallets|ops|mcp|public|config|stripe|llm|health|metrics|onboarding|disputes|reputation|runs|webhooks|skills|openapi.json)(/|$) {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    # SPA fallback for client-side routes
    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

### Useful server commands

```bash
# Live logs
sudo journalctl -u aztea -f

# Last 100 lines of logs
sudo journalctl -u aztea -n 100

# Restart API
sudo systemctl restart aztea

# Force kill if stuck (background threads blocking shutdown)
sudo systemctl kill -s SIGKILL aztea && sudo systemctl start aztea

# Check service status
sudo systemctl status aztea

# Manual DB backup
sqlite3 /path/to/registry.db ".backup /path/to/registry.db.bak"

# Open a Python shell with app context
cd /home/aztea/app && source venv/bin/activate && python

# Run reconciliation manually
curl -H "Authorization: Bearer $API_KEY" -X POST https://aztea.ai/ops/payments/reconcile
```

### Environment variables (prod)

Stored in `.env` on the server (never committed). Key vars:

```
# Core
ENVIRONMENT=production
API_KEY=                        # master key — openssl rand -hex 32
SERVER_BASE_URL=https://aztea.ai
FRONTEND_BASE_URL=https://aztea.ai
CORS_ALLOW_ORIGINS=https://aztea.ai
AZTEA_FRONTEND_URL=https://aztea.ai

# Stripe (use live keys in prod)
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PUBLISHABLE_KEY=pk_live_...

# LLM (at least one required)
GROQ_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
AZTEA_LLM_DEFAULT_CHAIN=groq,openai,anthropic

# Optional features
AZTEA_ENABLE_LIVE_DISPUTE_JUDGES=1
AZTEA_ENABLE_LIVE_QUALITY_JUDGE=1

# Email (if unset, all email silently no-ops)
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=noreply@aztea.ai
```

### Stripe webhook

Endpoint: `POST https://aztea.ai/stripe/webhook`
Register in the Stripe dashboard; set `STRIPE_WEBHOOK_SECRET` to the signing secret.
Required events: `checkout.session.completed`, `payment_intent.succeeded`.

---

## Critical invariants — never violate these

### Money

- **Integer cents only.** Never store or pass floats for money. `price_per_call_usd` in specs is float for display only; the ledger always uses `*_cents INTEGER`.
- **Insert-only ledger.** `transactions` gets only INSERT, never UPDATE or DELETE. Corrections are compensating entries.
- **Double-settlement guard.** `pre_call_charge`, `post_call_payout`, and `post_call_refund` each have race guards (rowcount checks on wallet UPDATE). Every new settlement path must replicate the guard.
- **Dispute atomicity.** Dispute insert + escrow clawback MUST happen in one SQLite transaction. Lock failure rolls back the dispute row — see `core/disputes.py`.
- **Payout-curve clawbacks** use `charge`/`refund` ledger types only — never custom transaction types. Idempotency key: `payout_curve:{job_id}`. See `core/payout_curve.py`.
- **wallets.balance_cents is a cache.** It must be updated in the same SQL transaction as the ledger row that changes it. Validated by reconciliation runs (`POST /ops/payments/reconcile`).

### Database

- **Single connection manager.** All modules use `core/db.py`. Never open a raw `sqlite3.connect()` anywhere.
- **WAL mode + thread-local pool.** `DB_MAX_CONNECTIONS` (default 32) caps connections. Network I/O to downstream agents happens **between** transactions — never hold a write lock during an HTTP call.
- **`caller_ratings` lives only in `reputation.py`.** `disputes.py` does not declare it. Do not re-declare or migrate this table elsewhere.
- **Migrations are idempotent.** Each `.sql` file is applied once via a `schema_migrations` table. Never re-use a migration filename; always add a new one.

### Auth & security

- **Scoped keys:** `caller`, `worker`, `admin`, plus agent-scoped worker keys (`azac_...`). Every mutation route checks scope and ownership.
- **API key values are never logged.** Log only the prefix (`az_xxx...`). Automatic redaction is in `logging_utils.py`.
- **All outbound URLs go through `url_security.py`** (agent endpoints, verifiers, webhooks, onboarding URLs, git clone paths). Private IPs, loopback, IPv6, and URL-encoded bypass chars are blocked. Dev override: `ALLOW_PRIVATE_OUTBOUND_URLS=1`.

### LLM layer

- **`LLMResponse.text` — not `.content`.** Every agent module must use `raw.text`. Using `.content` silently returns `None` at runtime.
- **Never pass `model=` to `CompletionRequest` when using `run_with_fallback`.** The fallback chain selects the model. Pass `model=""` or omit it.
- **Provider-agnostic.** Don't hardcode a provider or model in any built-in agent. Use `run_with_fallback(req)` which tries `AZTEA_LLM_DEFAULT_CHAIN` (env-overridable).
- **Graceful LLM degradation.** If synthesis fails because no LLM provider is configured, agents that performed real retrieval must still return the retrieval output rather than raising an exception. See `agents/arxiv_research.py` for the pattern.

### Built-in agents

- Agent IDs are **deterministic UUID v5** from namespace `6ba7b810-9dad-11d1-80b4-00c04fd430c8` + `aztea.builtin.{slug}`. Constants live in `server/builtin_agents/constants.py`.
- **Only agents with real tool use go in `CURATED_PUBLIC_BUILTIN_AGENT_IDS`.** LLM wrappers that add no value over a direct chat session must not be in the curated set. The six deprecated agents (`github_fetcher`, `pr_reviewer`, `test_generator`, `spec_writer`, `changelog_agent`, `package_finder`) sunset on **2026-07-26** — do not add new ones of this type.
- Each new built-in agent needs: module in `agents/`, entry in `BUILTIN_INTERNAL_ENDPOINTS`, spec in `specs_part1.py` or `specs_part2.py`, case in `_execute_builtin_agent()`, and a structured error envelope (see "Adding a new built-in agent").
- **Work examples** are stored via `_record_public_work_example()`. Pass `private_task=True` to skip recording. Ring buffer capped at `_AGENT_WORK_EXAMPLES_MAX`.

### MCP surface

- Tool names are plain `snake_case` from the agent name — no prefix.
- All manifest keys use `snake_case` (`input_schema`, `output_schema`, `price_per_call_usd`).
- `/mcp/invoke` authenticates via `auth.verify_agent_api_key` or a caller-scoped user key.
- `scripts/aztea_mcp_server.py` refreshes tools every 60s via the HTTP registry.

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
POST /jobs/{id}/rating          caller → rates agent (triggers payout-curve clawback if configured)
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

**Native providers:** groq, openai, anthropic, cohere, bedrock (all others via `openai_compatible_provider.py`)

**25+ pre-configured compatible providers:** mistral, together, fireworks, deepseek, perplexity, cerebras, openrouter, sambanova, novita, ai21, deepinfra, hyperbolic, anyscale, nvidia, lmstudio, ollama, azure, and more.

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

- **React 18 + Vite + motion/react** (`framer-motion` fork) for animations
- **CSS variables** for theming in `src/theme/tokens.css` — never hardcode colours or spacing
- **Feature-based structure:** `src/features/agents/`, `src/features/jobs/`, `src/features/auth/`, etc.
- **UI primitives** in `src/ui/` (Button, Pill, Segmented, Input, Card, Badge, etc.) — always use these, never raw HTML equivalents
- **Motion primitives** in `src/ui/motion/` (Reveal, Stagger, NumberMorph, ContainerScroll, etc.) — use for all animations, never raw `motion()` calls
- **`src/api.js`** — all API calls go through here
- **`ResultRenderer`** in `src/features/agents/results/` — handles rich output display
- **Error handling pattern:** every user action must show inline errors (not just toasts); toasts are for success only
- **Aesthetic rule:** never use Inter/Roboto/Arial; never use purple gradients; commit to a cohesive theme with distinctive typography, dominant colours with sharp accents, and intentional motion at load time
- **Known tech debt:** `fmtDate`, `fmtUsd`, `fmtMs`, and `relativeTime` are copy-pasted into 10+ page files. A shared `src/utils/format.js` should be the canonical location — consolidate on next touch
- **Inline styles:** many pages (esp. `JobDetailPage`, `DashboardPage`) use `style={{}}` objects instead of CSS classes. Prefer CSS classes with token variables on every new or edited component

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

# Tests (453 passed + 1 skipped on main suite as of 2026-04-28;
# run the SDK contract suite separately — it can segfault under Python 3.14 on macOS)
pytest -q tests --ignore=tests/test_sdk_contract.py
pytest -q tests/test_sdk_contract.py

# Integration tests only (137 passed as of 2026-04-28)
pytest -q tests/integration

# Line-budget enforcement (every Python source file must be < 1000 lines)
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

**Current test status:** `pytest tests --ignore=tests/test_sdk_contract.py` → **453 passed, 1 skipped**. `pytest tests/integration` → **137 passed**. The skipped test is intentional (feature flag–gated).

---

## Operational runbooks

Runbooks for the three highest-risk operational scenarios live in `docs/runbooks/`:

- **`docs/runbooks/ledger-drift.md`** — what to do when reconciliation reports non-zero drift or mismatch count; step-by-step query guide to trace the root cause
- **`docs/runbooks/runtime-prerequisites.md`** — which agents require which system packages (Playwright/chromium, Node, Deno, Go, Rust, ruff, mypy, tsc) and how to verify they are present
- **`docs/runbooks/buyer-surface-smoke-test.md`** — ordered smoke-test checklist to verify all buyer surfaces (web, MCP/Claude, Python SDK, CLI, TUI, REST) are functioning after a deploy

Update the relevant runbook in the same commit as any change that affects money flows, adds a runtime dependency, or changes a buyer surface.

---

## Package distribution (PyPI + npm)

Publish order is important:

1. Publish `aztea-tui` first (`tui/pyproject.toml`).
2. Publish `aztea` second — it depends on `aztea-tui`, so the new TUI version must be on PyPI first.

```bash
# 1) TUI (PyPI)
cd tui
python3 -m venv .release-venv && source .release-venv/bin/activate
python -m pip install -U pip build twine
python -m build
python -m twine upload dist/aztea_tui-*

# 2) SDK (PyPI)
cd ../sdks/python-sdk
source ../../tui/.release-venv/bin/activate
python -m build
python -m twine upload dist/aztea-*

# 3) npm wrapper
cd ../../tui/npm
npm publish --access public --otp <code>
```

Quick verification in a clean environment:

```bash
python3 -m venv /tmp/aztea-check && source /tmp/aztea-check/bin/activate
pip install -U aztea
python -c "import aztea; print(aztea.__version__)"
which aztea-tui
```

---

## Adding a new built-in agent

1. Create `agents/{slug}.py` with a `run(payload: dict) -> dict` function and a module-level docstring that describes inputs, outputs, external dependencies, and runtime requirements.
2. Generate a stable ID: `uuid.uuid5(uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8'), 'aztea.builtin.{slug}')`.
3. Add the ID as a constant in `server/builtin_agents/constants.py` and wire into `BUILTIN_INTERNAL_ENDPOINTS` + `CURATED_PUBLIC_BUILTIN_AGENT_IDS` (only if the agent performs real external work beyond pure LLM prompting).
4. Add the agent import to `server/application_parts/part_000.py` (the import shard).
5. Add a case to `_execute_builtin_agent()` — `grep -n "_execute_builtin_agent" server/application_parts/part_*.py` to find it.
6. Add a spec entry to `server/builtin_agents/specs_part1.py` **or** `specs_part2.py` (keep each under ~900 lines). The final curated list is assembled by `server/builtin_agents/specs.py::builtin_agent_specs()`.
7. Return a structured error envelope on failure — `{"error": {"code": "...", "message": "..."}}` — not a raw exception.
8. Handle the no-LLM case: if the agent fetches real data then synthesises with an LLM, it must return the raw data if LLM synthesis fails rather than raising.
9. Run `pytest tests/integration/test_hooks_builtin_mcp.py -q` to confirm registration + MCP manifest pick up the new agent.

**Agents earn a place in the public marketplace by doing something Claude can't do in a chat session.** Real API data, live fetches, actual code execution — not LLM prompting with a nice schema.

### Editing a shard (`server/application_parts/part_NNN.py`)

The shards share a single logical namespace — `server/application.py` compiles each shard in order into its own module globals. Practical rules:

- Add new imports to **`part_000.py`** (the import shard); other shards reference symbols already in scope.
- Add new top-level routes at the end of the shard that naturally owns the concern.
- Keep each shard **< 900 lines**. CI fails on any file > 1000 lines.
- If a function grows too large, move it into a helper module under `core/` — do **not** re-split the shards by hand.
- Every shard must begin with a `# server.application shard N — <what it owns>` comment so grep and human readers can orient quickly.

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

---

## Public agent IDs (current)

Curated public set — agents that perform real external work:

| Agent                       | ID                                     |
| --------------------------- | -------------------------------------- |
| CVE Lookup                  | `a3e239dd-ea92-556b-9c95-0a213a3daf59` |
| arXiv Research              | `9e673f6e-9115-516f-b41b-5af8bcbf15bd` |
| Python Code Executor        | `040dc3f5-afe7-5db7-b253-4936090cc7af` |
| Web Researcher              | `32cd7b5c-44d0-5259-bb02-1bbc612e92d7` |
| Image Generator             | `4fb167bd-b474-5ea5-bd5c-8976dfe799ae` |
| Code Review                 | `8cea848f-a165-5d6c-b1a0-7d14fff77d14` |
| DNS Inspector               | `3d677381-791c-5e83-8e66-5b77d0e43e2e` |
| Dependency Auditor          | `11fab82a-426e-513e-abf3-528d99ef2b87` |
| Multi-File Executor         | `ea95cdec-32c1-5a2b-a032-3e7061abf3a4` |
| Linter                      | `7ec4c987-9a7e-5af8-984f-7b8ad0ad0536` |
| Shell Executor              | `6bd98167-e010-5604-8c76-6ed1b92698f1` |
| Type Checker                | `5b140628-52a8-565b-8599-b1c3e402b02d` |
| DB Sandbox                  | `be4d6c18-629d-5b1c-8c46-f82c00db4995` |
| Visual Regression           | `20a74467-d633-5016-b210-adf769b2df9c` |
| Live Endpoint Tester        | `8af9fc34-ec0c-5732-b0e0-4e4efdff749c` |
| Browser Agent               | `c3a1b2d4-e5f6-5a7b-8c9d-0e1f2a3b4c5d` |
| Multi-Language Executor     | `d4b2c3e5-f6a7-5b8c-9d0e-1f2a3b4c5d6e` |
| Semantic Codebase Search    | `e5c3d4f6-a7b8-5c9d-0e1f-2a3b4c5d6e7f` |
| AI Red Teamer               | `f6d4e5a7-b8c9-5d0e-1f2a-3b4c5d6e7f8a` |

Internal / special purpose:

| Agent                       | ID                                     |
| --------------------------- | -------------------------------------- |
| Quality Judge (internal)    | `9cf0d9d0-4a10-58c9-b97a-6b5f81b1cf33` |
| Financial Research (legacy) | `b7741251-d7ac-5423-b57d-8e12cd80885f` |
| Wikipedia Research (legacy) | `9a175aa2-8ffd-52f7-aae0-5a33fc88db83` |

Deprecated — sunset 2026-07-26 (kept for backward compat, excluded from marketplace):

| Agent             | ID                                     |
| ----------------- | -------------------------------------- |
| GitHub Fetcher    | `5896576f-bbe6-59e4-83c1-5106002e7d10` |
| HN Digest         | `31cc3a99-eca6-5202-96d4-8366f426ae1d` |
| PR Reviewer       | `3e133b66-3bc6-5003-9b64-3284b28a60c6` |
| Test Generator    | `f515323c-7df2-5742-ac06-bc38b59a40cb` |
| Spec Writer       | `ce9504a3-74c8-51a5-913e-6ae55787abc8` |
| Changelog Agent   | `48c24ce5-d9cb-5f76-9e2f-fce1878f8c4c` |
| Package Finder    | `d11ddab1-bcca-55de-8b00-c9efadc69c79` |
