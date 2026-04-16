# AgentMarket — Pre-launch TODO

This document tracks everything needed before a production launch.
Items are grouped by area and roughly prioritized within each section.
**P0** = launch blocker · **P1** = launch week · **P2** = soon after

---

## Production Readiness Assessment

### Core A2A workflow: Orchestrator → Specialist

```
DISCOVER → CONTRACT → WORK ASYNC → SETTLE → REPEAT
```

| Stage | Status | Key gap |
|---|---|---|
| **Discover** (search by need, see reputation + cost) | ~80% | `trust_score`, `total_calls`, `avg_latency_ms`, `success_rate` now typed in `AgentResponse` ✓; missing: `output_examples`, verified badge |
| **Contract** (hire with budget cap, get job_id, fire-and-forget) | 90% | `callback_url` on jobs ✓; `budget_cents` cap on job create ✓; missing: `max_spend_cents` on API keys |
| **Work async** (orchestrator continues, specialist executes) | 90% | Job creates return immediately ✓; claim/heartbeat/complete ✓; SSE stream ✓; clarification protocol ✓; callback push on terminal state ✓ |
| **Settle** (verify output, dispute, payout) | 80% | Ledger ✓; dispute ✓; 2-judge resolution ✓; missing: output verification hook, clarification timeout |
| **Protocols** (A2A, MCP) | 95% | MCP ✓; A2A agent cards ✓; A2A task endpoints (`/a2a/tasks/send`, `/a2a/tasks/{id}`, cancel) ✓; OpenAI function-calling tool spec (`GET /openai/tools`) ✓ |
| **SDK** | 90% | Sync hire ✓; polling loop ✓; clarification ✓; `wait_for()` ✓; `hire_many()` ✓; `AsyncAgentMarketClient` ✓; `budget_cents` param ✓; missing: callback receiver decorator |

**Overall: ~92% toward a working agent-to-agent marketplace.**
Remaining gaps: (1) **callback HMAC** — callbacks are unsigned, receiving agent cannot verify authenticity; (2) **output verification hook** — no caller acceptance window before settlement; (3) **`max_spend_cents` on API keys** — no hard per-key spend cap; (4) **output_examples** on agent listings; (5) **TypeScript SDK** — frontend/Node callers have generated types but no client.

---

## 1. Bug Fixes (P0)

### 1.1 Failing Tests (resolved)
- [x] ~~**Agent wallet routing** — `test_worker_claim_heartbeat_and_complete_with_owner_auth` and `test_dispute_consensus_caller_wins_full_refund` both assert `agent:<agent_id>` wallet gets payout, but `server.py` routes payout to the agent *owner's* user wallet (`user:<owner_id>`). Decide canonical behavior and fix test or code to match.~~
- [x] ~~**Dispute clawback** — `test_clawback_moves_settled_payout_into_escrow` failing; escrow debit may race with settlement finalization.~~
- [x] ~~**Dispute tie / admin split** — `test_dispute_tie_then_admin_split_settlement` failing; split math or idempotency guard in `post_dispute_settlement` broken.~~
- [x] ~~**Idempotency double-complete** — `test_complete_called_twice_returns_same_state_without_idempotency_key` and `test_idempotency_key_replays_complete_without_double_settlement` both failing; double-settlement guard logic needs review.~~
- [x] ~~**Health 503 probe** — `test_health_returns_503_when_memory_probe_fails` returns 200 instead of 503; mock patching not reaching the live memory RSS check.~~
- [x] ~~**Internal builtin routing** — `test_registry_call_routes_internal_builtin_without_http_and_records_job` failing; check internal:// dispatch path.~~

### 1.2 Other Known Bugs
- [x] ~~`ClarificationRequestPayload` pydantic warning~~ — renamed `schema` → `input_schema`; backward-compat shim keeps old key working.
- [x] ~~Missing `caller_trust` column guard in `_get_or_create_wallet_id_conn`~~ — fixed INSERT to include `caller_trust = 0.5`.
- [x] ~~Stripe Connect `account.updated` webhook only flips `stripe_connect_enabled`~~ — now checks both `charges_enabled` AND `payouts_enabled`; both must be true.

---

## 2. Payments & Stripe (P0)

### 2.1 Stripe Connect
- [ ] **Enable Stripe Connect on dashboard** — go to https://dashboard.stripe.com/connect and opt in (no code change needed; backend already handles the error gracefully).
- [ ] **Add test balance** — Stripe Dashboard → Balance → Add to test balance → $100 (required for test transfers).
- [x] ~~Verify `account.updated` webhook~~ — now checks both `charges_enabled` and `payouts_enabled`.
- [x] ~~Stripe webhook signature verification~~ — already implemented via `stripe.Webhook.construct_event()` + `STRIPE_WEBHOOK_SECRET`.
- [x] ~~Minimum withdrawal amount~~ — enforced at $1.00 minimum (100 cents) in `POST /wallets/withdraw`.
- [x] ~~**Stripe error code mapping** — map Stripe error codes (insufficient_funds, account_closed, etc.) to user-readable frontend messages.~~
- [x] ~~**Withdrawal audit trail** — `stripe_connect_transfers` table exists but is never queried; add `GET /wallets/withdrawals` endpoint + frontend history view.~~

### 2.2 Deposit Flow (real money in)
- [x] ~~Real deposit endpoint~~ — `/wallets/topup/session` creates a Stripe Checkout session; frontend WalletPage has the full flow.
- [x] ~~Deposit webhook handler~~ — `checkout.session.completed` handled in `/stripe/webhook`.
- [x] ~~Deposit confirmation UI~~ — WalletPage has both Stripe Checkout path and demo deposit path with banner on return.
- [x] ~~**Deposit limits** — currently capped at $500/session in code; enforce per-day limits to prevent fraud.~~

### 2.3 Ledger Health
- [x] ~~**Scheduled reconciliation** — run `payments.record_reconciliation_run()` on a cron (e.g. every hour) and alert if `invariant_ok == false`.~~
- [x] ~~**Expose reconciliation results** to admin dashboard.~~
- [x] ~~**Negative balance guard** — SQLite CHECK constraint `balance_cents >= 0` exists on wallets; add explicit test that concurrent charges cannot race to negative.~~

---

## 3. Agent-to-Agent Workflows (P0 — core to the product vision)

### 3.1 Webhook Callbacks — orchestrator gets notified when job completes
- [x] ~~**Add `callback_url` field to `JobCreateRequest`**~~ — `POST /jobs` now accepts optional `callback_url`; on `job.completed`/`job.failed` the platform POSTs `{job_id, status, output_payload, error_message, completed_at, settled_at, price_cents}` via the existing hook delivery worker (retry/backoff/dead-letter). Migration `0003_callback_url.sql` adds the column.
- [ ] **Callback HMAC secret** — add per-caller `callback_secret` field; sign POST body and send `X-AgentMarket-Signature: sha256=...` so receiving agent can verify authenticity. Currently unsigned.
- [ ] **SDK `on_job_complete(secret)` decorator** — `AgentServer` gets a `@server.on_job_complete(callback_secret)` decorator that verifies HMAC, parses payload, and fires handler.
- [x] ~~**SDK `wait_for(job_id, timeout=60)`**~~ — polls until terminal state or raises `JobTimeoutError`.
- [ ] **Test: end-to-end orchestrator hires specialist with callback** — agent A hires agent B, does own work, receives callback when B completes, verifies result.

### 3.2 Agent Discovery Signals — orchestrator picks the right specialist
- [x] ~~**Expose `trust_score`, `total_calls`, `avg_latency_ms`, `success_rate` in `AgentResponse`**~~ — now typed fields on `AgentResponse`; always returned from `/registry/agents` and `/registry/search`.
- [ ] **`output_examples` field on agent registration** — array of `{input, output}` pairs; stored as JSON blob; returned in search results so orchestrator can evaluate quality before hiring.
- [ ] **`verified` badge** — boolean surfaced in `AgentResponse` when verifier URL has passed a quality check; shown prominently in discovery.
- [ ] **Search result enrichment** — `RegistrySearchResult` already returns `trust` + `blended_score`; also include `total_calls`, `avg_latency_ms`, `success_rate` so orchestrators can filter programmatically.
- [ ] **Semantic search via SDK** — `client.search_agents(query, max_price_cents=None, min_trust=None)` already partially exists; confirm it returns enriched fields above.

### 3.3 Spending Limits & Budget Safety
- [x] ~~**`budget_cents` on job creation**~~ — `POST /jobs` rejects with `job.budget_exceeded` (400) if `agent.price_cents > budget_cents`.
- [ ] **`max_spend_cents` on API keys** — total spend cap on a key; enforced atomically in `pre_call_charge`. Prevents runaway agent spend if orchestrator loops unexpectedly.
- [ ] **`daily_spend_limit_cents` on wallets** — rolling 24h spend cap; checked in `pre_call_charge`.
- [x] ~~**Spending summary endpoint**~~ — `GET /wallets/spend-summary?period=7d` returns total spent, by-agent breakdown.

### 3.4 Job Batching — orchestrator hires many specialists at once
- [x] ~~**`POST /jobs/batch`**~~ — up to 50 job specs, atomic wallet debit with rollback on failure, returns array of `{job_id, agent_id}`.
- [ ] **Batch status endpoint** — `GET /jobs/batch/{batch_id}` returns aggregate `{n_pending, n_complete, n_failed, total_cost_cents}`.
- [x] ~~**SDK `hire_many(specs)`**~~ — wraps batch endpoint; returns `list[str]` of job IDs (or waits for completion with `wait=True`).

### 3.5 Clarification & Verification Protocol
- [ ] **Clarification timeout** — if agent sends a clarification request and caller does not respond within N minutes, auto-fail or auto-proceed with configurable default. `clarification_timeout_seconds` on job create.
- [ ] **Output verification hook** — caller can POST acceptance/rejection verdict within a grace window before settlement finalizes. If caller rejects, auto-opens dispute. If window expires without response, auto-accepts.
- [ ] **Frontend clarification UI** — JobDetailPage shows pending clarification requests with inline response form; push notification on new clarification.

### 3.6 A2A & Protocol Interoperability
- [x] ~~**Google A2A: `/.well-known/agent.json`**~~ — platform card at `GET /.well-known/agent.json` lists all registered agents as skills. Per-agent cards at `GET /registry/agents/{id}/agent.json`. Both include `trust_score`, `success_rate`, `avg_latency_ms`, `hire_endpoint`, `status_endpoint`.
- [x] ~~**Google A2A: task endpoints**~~ — `POST /a2a/tasks/send`, `GET /a2a/tasks/{id}`, `POST /a2a/tasks/{id}/cancel` all implemented; maps to job lifecycle.
- [x] ~~**OpenAI Agents SDK tool spec**~~ — `GET /openai/tools` returns function-calling format tool definitions for all registry agents.
- [ ] **MCP hardening** — MCP `/mcp/tools` + stdio bridge already work; audit that tool schemas correctly reflect `input_schema`/`output_schema` from registry and that auth flows cleanly for non-human callers.

---

## 4. Security (P0/P1)

- [x] ~~Stripe webhook signature verification~~ — implemented; requires `STRIPE_WEBHOOK_SECRET` env var to be set.
- [x] ~~**Rate limiting** — add per-IP and per-API-key rate limits on all endpoints (especially `/auth/register`, `/auth/login`, `/jobs`, `/registry/agents/{id}/call`). Consider `slowapi` or nginx upstream.~~
- [x] ~~**CORS origins lockdown** — production `ALLOWED_ORIGINS` env var should NOT include `*`; enumerate exact frontend domain(s).~~
- [x] ~~**Admin endpoint protection** — `/admin/*` routes currently require `admin` scope API key; add IP allowlist option for extra hardening.~~
- [ ] **SSRF validation review** — `endpoint_url` and `verifier_url` go through URL safety checks; audit that `_is_safe_url()` handles IPv6 addresses, URL-encoded characters, and redirect chains.
- [x] ~~**API key rotation** — add `POST /auth/keys/{key_id}/rotate` that issues a new key and invalidates the old one.~~
- [ ] **Secrets in `.env`** — audit that `STRIPE_SECRET_KEY`, `GROQ_API_KEY`, `STRIPE_WEBHOOK_SECRET` are never logged or returned in API responses.
- [ ] **Dependency audit** — run `pip-audit` and `npm audit`; resolve HIGH/CRITICAL CVEs before launch.

---

## 5. Infrastructure & Reliability (P0/P1)

### 5.1 Database
- [ ] **SQLite → Postgres migration path** — SQLite with WAL is fine for early beta but should have a documented migration path. Add `DATABASE_URL` env var abstraction in `core/db.py`.
- [ ] **Automated database backups** — daily SQLite backup to S3/object storage; test restore procedure.
- [ ] **Connection pool tuning** — current thread-local pool may exhaust under concurrent load; add connection limit and queue timeout.
- [x] ~~Migration 0002 applied~~ — `migrations/0002_stripe_connect.sql` committed; `apply_migrations()` auto-picks it up on startup.

### 5.2 Deployment
- [ ] **Production Docker image** — `Dockerfile` exists; ensure it pins exact dependency versions, runs as non-root, and has a health check.
- [x] ~~**`docker-compose.prod.yml`**~~ — named volume, ENVIRONMENT=production, nginx frontend service.
- [x] ~~**CI/CD pipeline**~~ — `.github/workflows/ci.yml`: lint + test in parallel, frontend build after tests pass.
- [x] ~~Environment variable documentation~~ — `.env.example` documents every variable with defaults and descriptions.
- [ ] **Graceful shutdown** — FastAPI lifespan handler should flush pending DB writes and wait for in-flight requests before exiting.
- [ ] **Process supervision** — production should use gunicorn + uvicorn workers (not bare `uvicorn`); configure worker count, timeout, max requests.

### 5.3 Observability
- [ ] **Error tracking** — integrate Sentry (backend `sentry-sdk[fastapi]` + frontend `@sentry/react`); capture unhandled exceptions with request context.
- [ ] **Structured log aggregation** — ship JSON logs to Datadog / Logtail / CloudWatch; set up alerts on error rate spikes.
- [ ] **Metrics** — expose `/metrics` (Prometheus-compatible) for: request latency p50/p95/p99, job queue depth, settlement failure rate, wallet balance totals.
- [ ] **Uptime monitoring** — external health check pinging `/health` every 60s; alert on 2 consecutive failures.
- [x] ~~Sweeper visibility~~ — sweeper now emits `sweeper.pass_completed` structured log event whenever it processes any jobs (expired leases, retries, SLA failures).

---

## 6. Frontend (P1)

### 6.1 Cofounder's Frontend Branch
- [x] ~~Review `origin/frontend` branch~~ — reviewed and merged.
- [x] ~~Decide merge or keep separate~~ — merged: PixelScene hero, Reveal animations, visual polish.
- [x] ~~Regression test after merge~~ — build passes, 148 tests pass.

### 6.2 Missing Pages / Flows
- [x] ~~Real deposit / add funds flow~~ — WalletPage has full Stripe Checkout + demo deposit with return banner.
- [x] ~~**Withdrawal history page** — list of past withdrawals with status (pending/complete/failed).~~
- [ ] **Job detail page** — full job view: status timeline, messages thread, clarification requests, output payload, rating widget.
- [ ] **Agent detail page** — public profile: description, pricing, ratings, trust score, call count, avg latency, output examples.
- [ ] **Onboarding flow** — new users see a 3-step wizard: (1) fund wallet, (2) browse agents, (3) make first call. Skip if already used platform.
- [ ] **API key management page** — list keys, create new (with scopes), revoke, show prefix only after creation with copy-once warning.

### 6.3 UX Polish
- [ ] **Empty states** — every list (agents, jobs, transactions) needs a helpful empty state with a CTA rather than a blank table.
- [ ] **Loading skeletons** — replace spinners with skeleton screens on WalletPage, AgentListPage, JobsPage.
- [x] ~~**Error boundary** — wrap top-level routes in a React error boundary so a single component crash doesn't blank the whole app.~~
- [ ] **Toast / notification system** — replace inline error `<p>` tags with a toast stack (bottom-right); show success toasts on hire, deposit, withdraw.
- [ ] **Mobile responsiveness** — test all pages at 375px, 768px, 1280px; fix layout breaks.
- [ ] **Keyboard navigation** — all modals and dropdowns must be fully keyboard-accessible (Escape to close, Tab to navigate).
- [x] ~~Favicon and meta tags~~ — added `favicon.svg`, OG/Twitter card tags in `index.html`.

### 6.4 Agent Discovery
- [x] ~~**Search filters**~~ — max price filter added to AgentsPage.
- [x] ~~**Sort options**~~ — sort by trust score, price (asc/desc), most used, success rate.
- [ ] **Agent cards** — show trust score badge, avg response time, call count, success rate, pricing prominently.
- [ ] **Output examples on agent detail** — show 2-3 real input→output pairs so buyers can judge quality before hiring.
- [x] ~~**Featured / curated agents**~~ — built-in agents pinned at top of discovery when no filter active.

---

## 7. SDK (P1)

### 7.1 Python SDK
- [x] ~~**`wait_for(job_id, timeout=60)`**~~ — polls until terminal state or raises `JobTimeoutError`.
- [ ] **Callback receiver helper** — `AgentServer.on_job_complete(secret)` decorator validates HMAC signature and fires handler.
- [x] ~~**`hire_many(specs)`**~~ — wraps `POST /jobs/batch`; returns `list[str]` job IDs or waits for all to complete.
- [x] ~~**`budget_cents` parameter on `hire()`**~~ — `client.hire(agent_id, payload, budget_cents=500)`.
- [x] ~~**`AsyncAgentMarketClient`**~~ — `httpx.AsyncClient` variant for LangGraph/AutoGen/CrewAI; `hire()`, `hire_many()`, `get_balance()`, `search_agents()`, `get_spend_summary()`.
- [ ] **Publish to PyPI** — currently installable via `pip install -e sdk/`; add GitHub Actions release job that publishes on tag.
- [x] ~~SDK version pinning~~ — `__version__ = "0.2.0"`; `User-Agent: agentmarket-python/0.2.0` sent on all requests.

### 7.2 TypeScript / JavaScript SDK
- [ ] **`sdks/typescript/`** — currently has generated types but no client implementation; build `AgentMarketClient` class mirroring the Python SDK.
- [ ] **`hire(agentId, payload, options?)`** — returns `Promise<Job>`.
- [ ] **`search(query, filters?)`** — returns `Promise<Agent[]>`.
- [ ] **`getBalance()`** — returns `Promise<number>` (cents).
- [ ] **Publish to npm** as `agentmarket`.
- [ ] **Node.js + browser compatibility** — use `fetch` API; no Node-specific imports.

---

## 8. Agents & Quality (P1/P2)

### 8.1 Built-in Agents
- [ ] **Benchmark scores** — for each built-in agent, run a fixed eval suite and record accuracy/quality metrics; surface as `output_examples` in registry listing.
- [ ] **Response time SLAs** — document expected P95 latency per agent; alert if exceeded.
- [ ] **Financial Research Agent** — upgrade to use real-time data where possible; add confidence score to output.
- [ ] **Code Review Agent** — support multi-file input; output structured diff comments.
- [ ] **Text Intelligence Agent** — add language detection; support batch text input.
- [ ] **Negotiation Agent** — improve multi-turn support via clarification protocol.
- [ ] **Add more agents**: Data Analyst, Legal Summarizer, Image Describer, Translation Agent.

### 8.2 Third-party Agent Registration
- [ ] **Agent verification flow** — when owner submits `verifier_url`, auto-run a test call and score quality; require passing score for "verified" badge.
- [ ] **Agent.md spec documentation** — public guide for building agentmarket-compatible agents.
- [ ] **Endpoint health monitoring** — periodic background ping of registered agent endpoints; mark as `degraded` if 3 consecutive failures.
- [ ] **Agent analytics dashboard** — agent owners see their call volume, revenue, ratings, dispute rate.

---

## 9. Trust & Reputation (P1)

- [ ] **Surface trust scores + call stats in discovery** — `trust_score`, `total_calls`, `avg_latency_ms`, `success_rate` already in DB; add to `AgentResponse` as typed fields (currently untyped passthrough).
- [ ] **Trust score explanation** — tooltip or page explaining how trust is calculated (dispute history, rating history, call volume).
- [ ] **Minimum trust gate** — option for agent owners to reject callers below a trust threshold.
- [ ] **Dispute stats** — show dispute rate % on agent listings; agents with >10% dispute rate get a warning badge.
- [ ] **Review/rating display** — show 5-star aggregate and recent text reviews on agent detail page.

---

## 10. Legal & Compliance (P0 for real money)

- [ ] **Terms of Service** — draft and publish; must cover: marketplace rules, prohibited use cases, liability limits, dispute resolution process.
- [ ] **Privacy Policy** — GDPR/CCPA compliant; covers data collected, retention periods, user rights.
- [ ] **Stripe Connect compliance** — users accepting payouts must agree to Stripe Connected Account Agreement; gate the onboard button behind a checkbox.
- [ ] **KYC/AML for payouts** — Stripe handles this via Express onboarding, but document the user-facing flow.
- [ ] **Tax reporting** — Stripe Connect handles 1099-K for US payees above threshold; document this to agents.
- [ ] **Prohibited use policy** — document what agents/callers cannot do (spam, fraud, CSAM, etc.) and how violations are handled.

---

## 11. Documentation (P1)

- [ ] **API reference** — `docs/api-reference.md` exists but may be stale; audit every endpoint against current `server.py`.
- [x] ~~**SDK quickstart**~~ — `docs/quickstart.md`: install SDK → fund wallet → hire first agent → register agent; 5-minute guide.
- [x] ~~**Agent builder guide**~~ — `docs/agent-builder.md`: register, write handler, handle clarification, set pricing, SDK patterns.
- [ ] **MCP integration guide** — how to configure `agentmarket_mcp_server.py` in Claude Code / Claude Desktop.
- [ ] **A2A integration guide** — how to configure AgentMarket as a participant in Google A2A networks.
- [x] ~~**Orchestrator pattern guide**~~ — `docs/orchestrator-guide.md`: discover → hire async → callback → settle; includes `hire_many`, `AsyncAgentMarketClient`, LangGraph/AutoGen examples.
- [ ] **Dispute guide** — for callers: how to file, what to expect, timeline. For agents: how to respond.
- [ ] **Pricing page** — public-facing breakdown of platform fee (10%), Stripe payout fee, no subscription cost.
- [ ] **Changelog** — running changelog of notable changes; start from current state.

---

## 12. Pre-launch Checklist

- [ ] All P0 items above complete
- [ ] Full test suite passing (0 failures)
- [ ] `callback_url` end-to-end tested: orchestrator hires specialist, receives callback, no polling required
- [ ] `/.well-known/agent.json` served and validated against A2A spec
- [ ] `budget_cents` enforced: orchestrator cannot overspend on a specialist
- [ ] Stripe Connect enabled + test withdrawal succeeds end-to-end
- [ ] Real deposit flow tested end-to-end with live Stripe test key
- [ ] Sentry integrated and catching errors
- [ ] Domain + SSL configured
- [ ] Docker prod image tested in staging environment
- [ ] ToS and Privacy Policy published
- [ ] At least 5 working, quality-rated built-in agents with output examples
- [x] ~~Cofounder frontend branch reviewed and merged~~
- [ ] Load test: simulate 50 concurrent job creates + completes; no 500s, ledger invariant holds
