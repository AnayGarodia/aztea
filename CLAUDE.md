# Aztea — contributor guide

## What this is

Aztea is an AI agent labor marketplace: callers hire agents by the task, workers earn revenue, and the platform handles billing, escrow, settlement, trust, and dispute resolution transparently. Think Stripe + Upwork + Dun & Bradstreet — but for AI agents.

Architecture in one sentence: **FastAPI monolith on SQLite WAL, provider-agnostic LLM layer, async job lifecycle, insert-only ledger, MCP-native agent surface.**

Live at **https://aztea.ai**

---

## Repository map

```
server/                        # HTTP app package: ``application.py`` (routes + workers), ``error_handlers.py``, ``routes/``
agents/                        # Built-in agent implementations (one module each)
  financial/                   # SEC EDGAR fetcher + synthesizer
  wiki.py                      # Wikipedia API
  codereview.py                # Structured LLM-based code review
  cve_lookup.py                # NIST NVD live API
  arxiv_research.py            # arXiv live API + LLM synthesis
  python_executor.py           # Subprocess sandbox (real code execution)
  web_researcher.py            # HTTP fetch + HTML strip + LLM analysis
  image_generator.py           # OpenAI / Replicate image gen
  media_generation.py          # Shared media helpers (used by image/video agents)
  (others: suspended from public marketplace — LLM wrappers with no real tool use)
core/
  db.py                        # SQLite connection manager — WAL, thread-local pool, PRAGMAs
  migrate.py                   # Idempotent migration runner (apply_migrations)
  auth.py                      # users, scoped API keys, agent-scoped keys
  registry.py                  # agent listings, semantic search, embeddings cache
  mcp_manifest.py              # registry → MCP tool manifest (snake_case keys, no prefix)
  embeddings.py                # sentence-transformers backend
  jobs.py                      # async job lifecycle, claim/lease, retries, messages
  payments.py                  # wallets, insert-only ledger, settlement helpers
  disputes.py                  # disputes, judgments (does NOT declare caller_ratings)
  judges.py                    # LLM-based dispute + quality judge logic
  reputation.py                # trust scores; SOLE owner of caller_ratings table
  onboarding.py                # agent.md parsing/validation/ingestion
  models.py                    # Pydantic v2 request/response contracts
  error_codes.py               # machine-readable error taxonomy
  url_security.py              # SSRF validation for all outbound URLs
  llm/
    base.py                    # Message, CompletionRequest, LLMResponse, LLMProvider Protocol
    errors.py                  # LLMError, LLMRateLimitError, LLMTimeoutError, LLMBadResponseError
    registry.py                # PROVIDERS dict, resolve(spec), DEFAULT_CHAIN, list_providers()
    fallback.py                # run_with_fallback() — chain-tries, skips unavailable, retries on rate limit
    providers/                 # groq, openai, anthropic, cohere, bedrock, openai_compatible (25+ via env)
migrations/
  0001_initial.sql             # canonical schema — all CREATE TABLE / INDEX
  0002–0007_*.sql              # incremental additions (applied once on startup)
sdks/
  python-sdk/                  # AzteaClient (hire), AgentServer (@handler + polling loop)
  python/                      # Resource-oriented HTTP SDK
  typescript/                  # TypeScript SDK
frontend/                      # React 18 + Vite + motion
scripts/
  agentmarket_mcp_server.py    # stdio MCP server — refreshes tools every 60s
  client_cli.py                # CLI shim over Python SDK
tests/                         # pytest — 230+ tests across API, payments, jobs, LLM, SDK
docker-compose.yml             # dev compose (no SSL, mounts ./data)
docker-compose.prod.yml        # prod compose (nginx + API, named volume for DB)
nginx.prod.conf                # nginx reverse proxy — /api/* → FastAPI, /* → React SPA
Makefile                       # dev shortcuts: make dev / test / docker / migrate
```

---

## Production deployment

### Cloudflare + EC2 (typical)

- **DNS:** Point the hostname to the EC2 public IP (A/AAAA) or a suitable CNAME. With Cloudflare proxy (orange cloud) on, set SSL/TLS to **Full** or **Full (strict)**; **Full (strict)** needs a valid certificate on the origin (e.g. certbot + nginx).
- **Client IP:** Terminate at nginx, forward `X-Forwarded-For` / `X-Real-IP`, and configure `TRUSTED_PROXY_IPS` so `slowapi` and admin checks see the real client (include your nginx/Cloudflare hop as required).
- **Env URLs:** `SERVER_BASE_URL`, `FRONTEND_BASE_URL`, and `CORS_ALLOW_ORIGINS` must use the public `https://` site name, not the raw EC2 IP.

### Infrastructure

- **Server:** AWS EC2 Ubuntu — `/home/aztea/app`
- **Stack:** systemd service (`aztea.service`) running uvicorn directly — no Docker
- **Process:** `/home/aztea/app/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000 --workers 1`
- **Database:** SQLite WAL at the path set in `.env` (`DB_PATH`), on the host filesystem
- **Reverse proxy:** nginx on ports 80/443, proxies `/api/*` → uvicorn on 127.0.0.1:8000, serves `frontend/dist/` for everything else
- **SSL:** managed by certbot on the host; nginx handles termination

### Deploying a new version

SSH into the server, then:

```bash
cd /home/aztea/app

# 1. Pull latest code (stash first if any local changes exist)
git stash
git pull origin main
git stash pop

# 2. Rebuild the React frontend
cd frontend && npm ci && npm run build && cd ..

# 3. Restart the API (migrations run automatically on startup)
sudo systemctl kill -s SIGKILL aztea   # force-kill if stuck in shutdown
sudo systemctl start aztea

# 4. Verify
sudo systemctl status aztea
```

**If the service stops cleanly** (not stuck), you can use `restart` instead of kill+start:
```bash
sudo systemctl restart aztea
```

Migrations run automatically on startup via `core/migrate.py` — no manual step needed.

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
```

### Stripe webhook

The webhook endpoint is `POST https://aztea.ai/stripe/webhook` (path `/stripe/webhook` on the API; nginx often exposes it as the same path under the site origin).
Register it in the Stripe dashboard and set `STRIPE_WEBHOOK_SECRET` to the signing secret.
Required events: `checkout.session.completed`, `payment_intent.succeeded`.

---

## Critical invariants — never violate these

### Money

- **Integer cents only.** Never store or pass floats for money. `price_per_call_usd` in specs is float for display only; the ledger always uses `*_cents INTEGER`.
- **Insert-only ledger.** `transactions` table gets only INSERT, never UPDATE or DELETE. Do not modify balance by directly writing to `wallets.balance_cents` — that field is computed from ledger entries.
- **Double-settlement guard.** `pre_call_charge`, `post_call_payout`, and `post_call_refund` each have race guards. If you add a new settlement path, replicate the guard.
- **Dispute atomicity.** Dispute insert + escrow clawback MUST happen in one SQLite transaction. Lock failure rolls back the dispute row — see `core/disputes.py`.

### Database

- **Single connection manager.** All modules use `core/db.py`. Never open a raw `sqlite3.connect()` anywhere.
- **WAL mode + thread-local pool.** `DB_MAX_CONNECTIONS` (default 32) caps connections. HTTP calls to downstream agents happen **between** transactions — network I/O never holds a write lock.
- **`caller_ratings` lives only in `reputation.py`.** `disputes.py` does not declare it. Do not re-declare or migrate this table anywhere else.
- **Migrations are idempotent.** Each `.sql` file is applied once via a `schema_migrations` table. Never re-use a migration filename; add a new one.

### Auth & security

- **Scoped keys:** `caller`, `worker`, `admin`, plus agent-scoped worker keys. Every mutation route checks scope and ownership.
- **API key values are never logged.** Log only the prefix (`am_xxx...`). Automatic redaction is in `logging_utils.py`.
- **All outbound URLs go through `url_security.py`** (agent endpoints, verifiers, webhooks, onboarding URLs). Private IPs, loopback, IPv6, URL-encoded chars blocked. Dev override: `ALLOW_PRIVATE_OUTBOUND_URLS=1`.

### LLM layer

- **`LLMResponse.text` — not `.content`.** The response field is `.text`. Every agent module must use `raw.text`, not `raw.content`.
- **Never pass `model=` to `CompletionRequest` when using `run_with_fallback`.** The fallback chain selects the model. Pass `model=""` or let the default apply.
- **Provider-agnostic.** Don't hardcode a provider or model name in any built-in agent. Use `run_with_fallback(req)` which tries `AZTEA_LLM_DEFAULT_CHAIN` (env-overridable).

### Built-in agents

- Agent IDs are **deterministic UUID v5** from namespace `6ba7b810-9dad-11d1-80b4-00c04fd430c8` + `aztea.builtin.{slug}`. They live as constants at the top of `server.py`. Never use sequential dummy IDs.
- **Only agents with real tool use are in `_CURATED_PUBLIC_BUILTIN_AGENT_IDS`.** LLM wrappers that add no value over a direct chat session should remain in `_BUILTIN_INTERNAL_ENDPOINTS` but NOT in the curated public set.
- Each built-in agent needs: module in `agents/`, entry in `_BUILTIN_INTERNAL_ENDPOINTS`, spec in `_builtin_agent_specs()`, case in `_execute_builtin_agent()`.
- **Work examples** are stored via `_record_public_work_example()`. Set `private_task=True` in the job payload to skip recording. Ring buffer capped at `_AGENT_WORK_EXAMPLES_MAX`.

### MCP surface

- Tool names are plain `snake_case` from the agent name — no prefix.
- All manifest keys use `snake_case` (`input_schema`, `output_schema`, `price_per_call_usd`).
- `/mcp/invoke` authenticates via `auth.verify_agent_api_key` or a caller-scoped user key.
- `scripts/agentmarket_mcp_server.py` refreshes tools every 60s via the HTTP registry.

---

## Core flows (quick reference)

### Sync call: `POST /registry/agents/{id}/call`

1. Auth/scope check → listing validation → SSRF check
2. `pre_call_charge` (debit caller wallet, creates charge record)
3. If `internal://` endpoint → `_execute_builtin_agent()` directly (no HTTP)
4. Else → proxy to registered URL
5. Success → `_settle_successful_job` (payout split agent 90% / platform 10%)
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

Sweeper handles expired leases, timeouts, auto-retries. Built-in worker polls pending jobs every 2s.

### Job messages + lease effects

| `msg_type`               | Lease effect                                    |
| ------------------------ | ----------------------------------------------- |
| `clarification_request`  | → `awaiting_clarification`, no heartbeat needed |
| `clarification_response` | → resume `running`                              |
| `progress`               | extends lease by `heartbeat_interval`           |

### Trust / dispute

```
POST /jobs/{id}/rating          caller → rates agent
POST /jobs/{id}/rate-caller     agent → rates caller
POST /jobs/{id}/dispute         atomic: insert + clawback
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

**Usage in agents:**

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
- **CSS variables** for theming in `src/theme/tokens.css` — never hardcode colors
- **Feature-based structure:** `src/features/agents/`, `src/features/jobs/`, `src/features/auth/`, etc.
- **UI primitives** in `src/ui/` (Button, Pill, Segmented, Input, etc.) — always use these, never raw HTML equivalents
- **`src/api.js`** — all API calls go through here
- **`ResultRenderer`** in `src/features/agents/results/` handles rich output display
- **Error handling pattern:** every user action must show inline errors (not just toasts); toasts are for success confirmations only
- **Aesthetic rule:** Never use Inter/Roboto/Arial. Never use purple gradients. Commit to a cohesive theme with distinctive typography, dominant colors with sharp accents, and intentional motion at load time. One well-orchestrated stagger beats scattered micro-animations.

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

# Tests (should pass 230+ / 2 known pre-existing failures re: provider validation)
pytest -q tests

# Single integration test
pytest tests/integration/test_workers_jobs_core.py::test_worker_claim_heartbeat_and_complete_with_owner_auth -q

# Frontend prod build
cd frontend && npm run build

# Manual DB migration
python -m core.migrate

# MCP server (stdio)
python scripts/agentmarket_mcp_server.py
```

**Known pre-existing test failures (not regressions):**

- `test_get_agents_invalid_provider_raises` — provider validation intentionally relaxed for full agnosticism
- `test_api_filter_agents_invalid_provider` — same

---

## Adding a new built-in agent

1. Create `agents/{slug}.py` with a `run(payload: dict) -> dict` function.
2. Generate a stable ID: `uuid.uuid5(uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8'), 'aztea.builtin.{slug}')`.
3. Add constant at top of `server.py` (`_{NAME}_AGENT_ID`).
4. Add to `_BUILTIN_INTERNAL_ENDPOINTS` and `_BUILTIN_LEGACY_ROUTE_ENDPOINTS`.
5. Add to `_CURATED_PUBLIC_BUILTIN_AGENT_IDS` **only if it uses real external tools or compute** (not pure LLM).
6. Add case to `_execute_builtin_agent()`.
7. Add spec to `_builtin_agent_specs()` (name, description, input_schema, output_schema, output_examples, price_per_call_usd).
8. Add import at top of `server.py`.

**Agents earn a place in the public marketplace by doing something Claude can't do in a chat session.** Real API data, live fetches, actual code execution — not LLM prompting with a nice schema.

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
```

---

## Public agent IDs (current)

| Agent                    | ID                                     |
| ------------------------ | -------------------------------------- |
| Financial Research       | `b7741251-d7ac-5423-b57d-8e12cd80885f` |
| Code Review              | `8cea848f-a165-5d6c-b1a0-7d14fff77d14` |
| Wikipedia Research       | `9a175aa2-8ffd-52f7-aae0-5a33fc88db83` |
| CVE Lookup               | `a3e239dd-ea92-556b-9c95-0a213a3daf59` |
| arXiv Research           | `9e673f6e-9115-516f-b41b-5af8bcbf15bd` |
| Python Code Executor     | `040dc3f5-afe7-5db7-b253-4936090cc7af` |
| Web Researcher           | `32cd7b5c-44d0-5259-bb02-1bbc612e92d7` |
| Image Generator          | `4fb167bd-b474-5ea5-bd5c-8976dfe799ae` |
| Quality Judge (internal) | `9cf0d9d0-4a10-58c9-b97a-6b5f81b1cf33` |

---

## Ground rules

- **Never delete migrations.** Add new ones with the next sequence number.
- **Never force-push main.** Always create a new commit.
- **Never open raw `sqlite3.connect()`.** Use `core/db.py` exclusively.
- **Never store floats in the ledger.** Integer cents only.
- **Frontend errors must be inline.** Toasts for success; inline error state for failures.
