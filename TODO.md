# AgentMarket — Pre-launch TODO

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
| **Discover** | ~95% | core signals now present (`verified`, `trust_score`, `success_rate`, call/latency stats) |
| **Contract** | 95% | `callback_url` ✓; `budget_cents` ✓; `max_spend_cents` ✓; `daily_spend_limit_cents` ✓ |
| **Work async** | 95% | claim/heartbeat/complete ✓; SSE ✓; clarification ✓; callback push + HMAC ✓ |
| **Settle** | 96% | ledger ✓; dispute ✓; 2-judge ✓; dispute-window hold/release ✓; output verification decision window ✓ |
| **Protocols** | 98% | MCP manifest + stdio framing/auth/schema hardening ✓ |
| **SDK** | 96% | sync/async hire ✓; `hire_many` ✓; `wait_for` ✓; `budget_cents` ✓; callback signing + receiver helper ✓; missing: AgentServer callback decorator sugar |

**Overall: ~97% toward a working agent-to-agent marketplace.**
Remaining gaps: mostly launch operations (infra, security/audit, legal/compliance), plus product UX polish.

---

## 1. Launch Infrastructure (P0)

### 1.1 Domain & Hosting
- [ ] **Register domain** — pick and register `agentmarket.dev` (or preferred domain); configure DNS.
- [ ] **SSL/TLS certificate** — Let's Encrypt via Certbot or managed cert (Render/Railway/Fly handle this automatically).
- [ ] **Pick hosting provider** — deploy backend to Railway / Render / Fly.io / EC2; document the chosen stack.
- [ ] **Deploy frontend** — Vercel or Netlify for the React app; or serve via nginx on the same host. Configure `VITE_API_URL` env var to point to production backend.
- [ ] **Production `.env`** — inject secrets via hosting provider's secret manager, not a committed `.env` file. Confirm `STRIPE_SECRET_KEY`, `GROQ_API_KEY`, `STRIPE_WEBHOOK_SECRET` are set.
- [ ] **CDN / static assets** — serve frontend build artifacts from a CDN (Vercel/Netlify do this automatically; otherwise configure CloudFront or Cloudflare).
- [ ] **Staging environment** — mirror of prod for pre-release smoke testing.

### 1.2 Stripe Go-Live
- [ ] **Enable Stripe Connect on dashboard** — go to `https://dashboard.stripe.com/connect` and opt in.
- [ ] **Add test balance** — Stripe Dashboard → Balance → Add to test balance → $100 (required for test transfers).
- [ ] **Switch to live Stripe keys** — replace `sk_test_...` and `pk_test_...` with live keys in production env; update `STRIPE_WEBHOOK_SECRET` for live webhook endpoint.
- [ ] **Register live webhook endpoint** — add `https://yourdomain.com/stripe/webhook` in Stripe Dashboard under live mode.
- [ ] **Complete Stripe platform profile** — business name, address, bank account for platform payouts; required before live transfers work.
- [ ] **Test end-to-end deposit + withdrawal in live mode** with a small real amount.

---

## 2. Payments & Stripe (P0)

- [x] ~~**Settlement-pending escrow (inverted escrow fix)** — successful jobs now remain unsettled during dispute window and are released by sweeper after window close; dispute resolution handles unsettled and legacy-settled paths without double settlement.~~
- [x] ~~**Dispute filing deposit** — `POST /jobs/{id}/dispute` now charges 5% (min 5¢) from filer into dispute deposit escrow; disputes persist `filing_deposit_cents`; settlement releases deposit back on win/split/void and forfeits to platform on loss.~~
- [ ] **Price float → integer migration** — `price_per_call_usd` is stored as SQLite REAL; billing math uses `Decimal(str(value))` as workaround. Add `price_per_call_cents INTEGER` column, backfill, and cut over in a single migration.
- [ ] **SSRF validation review** — `endpoint_url` and `verifier_url` go through `_is_safe_url()`; audit handling of IPv6, URL-encoded chars, and redirect chains.
- [ ] **Secrets audit** — confirm `STRIPE_SECRET_KEY`, `GROQ_API_KEY`, `STRIPE_WEBHOOK_SECRET` are never logged or returned in API responses.
- [x] ~~**Free-credits first-run path** — `POST /auth/register` already credits $1.00 (100 cents) to new wallets on registration.~~

---

## 3. Agent-to-Agent Workflows (P0)

### 3.1 Webhook Callbacks
- [x] ~~**Callback HMAC secret** — `callback_secret` field on `JobCreateRequest`; backend signs POST body with `X-AgentMarket-Signature: sha256=...` header.~~
- [x] ~~**SDK `CallbackReceiver` helper** — `CallbackReceiver(secret)` class with `@receiver.on_job_complete` decorator and `dispatch(body, sig)` method. Also exports `verify_callback_signature()`.~~
- [x] ~~**Test: end-to-end orchestrator hires specialist with callback** — agent A hires agent B, does own work, receives callback when B completes, verifies result.~~

### 3.2 Agent Discovery Signals
- [x] ~~**`output_examples` field on agent registration** — stored as JSON blob; returned in search results and agent responses.~~
- [x] ~~**`verified` badge automation** — schema column added; auto-set to 1 after verifier_url passes a quality check.~~
- [x] ~~**Search result enrichment** — `client.search_agents()` returns `total_calls`, `avg_latency_ms`, `success_rate` alongside `trust_score`.~~
- [x] ~~**`dispute_rate` on agent listings** — computed as `disputes_filed / total_calls`; returned in all agent responses via reputation enrichment.~~

### 3.3 Clarification & Verification
- [x] ~~**Clarification timeout** — if agent sends a clarification request and caller does not respond within N minutes, auto-fail or auto-proceed. `clarification_timeout_seconds` on job create.~~
- [x] ~~**Output verification hook** — caller can POST acceptance/rejection within a grace window before settlement finalizes. Rejection auto-opens dispute; expired window auto-accepts.~~
- [x] ~~**Parent/child job linkage + cascade policy** — add `parent_job_id` on child jobs and explicit behavior for parent fail (fail children vs detach).~~
- [x] ~~**Frontend clarification UI** — JobDetailPage shows pending clarification requests with inline response form.~~

### 3.4 A2A & Protocol Interoperability
- [x] ~~**MCP hardening** — tool schemas now use snake_case and stdio framing/auth parsing has dedicated regression coverage.~~

---

## 4. Security (P0/P1)

- [ ] **SSRF validation review** — audit `_is_safe_url()` for IPv6 addresses, URL-encoded characters, and redirect chains on `endpoint_url` and `verifier_url`.
- [ ] **Secrets in env** — audit that `STRIPE_SECRET_KEY`, `GROQ_API_KEY`, `STRIPE_WEBHOOK_SECRET` are never logged or returned in API responses.
- [ ] **Dependency audit** — run `pip-audit` and `npm audit`; resolve HIGH/CRITICAL CVEs before launch.
- [x] ~~**Rate-limit auth endpoints** — already at 10/minute per IP via `_AUTH_RATE_LIMIT`.~~

---

## 5. Infrastructure & Reliability (P0/P1)

### 5.1 Database
- [ ] **SQLite → Postgres migration path** — SQLite with WAL is fine for early beta but document the migration path; add `DATABASE_URL` env var abstraction in `core/db.py`.
- [ ] **Automated database backups** — daily SQLite backup to S3/object storage; test restore procedure.
- [ ] **Connection pool tuning** — current thread-local pool may exhaust under concurrent load; add connection limit and queue timeout.

### 5.2 Deployment
- [x] ~~**Production Docker image** — non-root user, HEALTHCHECK, gunicorn + uvicorn workers with configurable `WEB_CONCURRENCY`/`GUNICORN_TIMEOUT`.~~
- [x] ~~**Graceful shutdown** — lifespan now signals worker stop events, drains in-flight HTTP requests, and joins background threads with configurable shutdown timeouts.~~
- [x] ~~**Process supervision** — Dockerfile uses gunicorn + UvicornWorker; `WEB_CONCURRENCY`, `GUNICORN_TIMEOUT`, `GUNICORN_MAX_REQUESTS` env vars configurable.~~

### 5.3 Observability
- [ ] **Error tracking** — integrate Sentry (`sentry-sdk[fastapi]` backend + `@sentry/react` frontend); capture unhandled exceptions with request context.
- [ ] **Structured log aggregation** — ship JSON logs to Datadog / Logtail / CloudWatch; set up alerts on error rate spikes.
- [ ] **Metrics endpoint** — expose `/metrics` (Prometheus-compatible) for request latency p50/p95/p99, job queue depth, settlement failure rate, wallet balance totals.
- [ ] **Uptime monitoring** — external health check pinging `/health` every 60s; alert on 2 consecutive failures.

---

## 6. Notifications & Email (P1)

- [ ] **Pick transactional email provider** — SendGrid, Postmark, or Resend; add `SMTP_*` / `SENDGRID_API_KEY` env var.
- [ ] **Email on job complete** — notify caller when their job finishes (include output summary and cost).
- [ ] **Email on deposit confirmed** — receipt after Stripe `checkout.session.completed`.
- [ ] **Email on dispute opened/resolved** — notify both parties.
- [ ] **Email on withdrawal processed** — confirmation with amount and expected arrival.
- [ ] **Welcome email** — sent on `POST /auth/register`; include quickstart link.
- [ ] **User notification preferences** — allow users to opt out of non-critical emails.

---

## 7. Frontend (P1)

### 7.1 Missing Pages / Flows
- [x] ~~**Job detail page** — now includes status timeline, output payload rendering, explicit clarification thread with response form, and rating/dispute actions.~~
- [x] ~~**Agent detail page** — now includes public profile metrics (trust, pricing, reliability stats) and `output_examples` rendering.~~
- [ ] **Onboarding flow** — 3-step wizard for new users: (1) fund wallet, (2) browse agents, (3) make first call. Skip if already active.
- [ ] **API key management page** — list keys, create new (with scopes), revoke, show prefix only after creation with copy-once warning.

### 7.2 UX Polish
- [ ] **Empty states** — every list (agents, jobs, transactions) needs a helpful empty state with a CTA.
- [ ] **Loading skeletons** — replace spinners with skeleton screens on WalletPage, AgentListPage, JobsPage.
- [ ] **Toast / notification system** — replace inline error `<p>` tags with a toast stack; success toasts on hire, deposit, withdraw.
- [ ] **Mobile responsiveness** — test all pages at 375px, 768px, 1280px; fix layout breaks.
- [ ] **Keyboard navigation** — all modals and dropdowns must be keyboard-accessible (Escape to close, Tab to navigate).

### 7.3 Agent Discovery
- [ ] **Agent cards** — show trust score badge, avg response time, call count, success rate, pricing prominently.
- [ ] **Output examples on agent detail** — show 2-3 real input→output pairs so buyers can judge quality before hiring.

---

## 8. SDK (P1)

### 8.1 Python SDK
- [ ] **Callback receiver helper** — `AgentServer.on_job_complete(secret)` decorator validates HMAC and fires handler.
- [ ] **Publish to PyPI** — add GitHub Actions release job that publishes on tag push.

### 8.2 TypeScript / JavaScript SDK
- [x] ~~**`sdks/typescript/`** — `AgentMarketClient` now exposes high-level `hire()`, `hireMany()`, `search()`, `getWallet()`, `getBalance()`, and `deposit()`.~~
- [ ] **Publish to npm** as `agentmarket`.
- [ ] **Node.js + browser compatibility** — use `fetch` API; no Node-specific imports.

---

## 9. Agents & Quality (P1/P2)

### 9.1 Built-in Agents
- [ ] **Benchmark scores** — run fixed eval suite per built-in agent; record accuracy/quality metrics; surface as `output_examples` in registry.
- [ ] **Response time SLAs** — document expected P95 latency per agent; alert if exceeded.
- [ ] **Financial Research Agent** — add confidence score to output; real-time data where possible.
- [ ] **Code Review Agent** — support multi-file input; output structured diff comments.
- [ ] **Text Intelligence Agent** — add language detection; support batch text input.
- [ ] **Negotiation Agent** — improve multi-turn support via clarification protocol.
- [ ] **Add more agents**: Data Analyst, Legal Summarizer, Image Describer, Translation Agent.

### 9.2 Third-party Agent Registration
- [x] ~~**Agent verification flow** — `/registry/register` now auto-calls `output_verifier_url` during registration and sets `verified=1` on pass.~~
- [ ] **Agent.md spec documentation** — public guide for building agentmarket-compatible agents.
- [x] ~~**Endpoint health monitoring** — sweeper now pings external endpoint URLs and tracks degraded health state after 3 consecutive failures (with recovery resets).~~
- [ ] **Agent analytics dashboard** — agent owners see call volume, revenue, ratings, dispute rate.

---

## 10. Trust & Reputation (P1)

- [ ] **Trust score explanation** — tooltip or page explaining how trust is calculated (dispute history, ratings, call volume).
- [ ] **Minimum trust gate** — option for agent owners to reject callers below a trust threshold.
- [ ] **Dispute stats** — show dispute rate % on agent listings; agents with >10% dispute rate get a warning badge.
- [ ] **Review/rating display** — show 5-star aggregate and recent text reviews on agent detail page.

---

## 11. Legal & Compliance (P0 for real money)

- [ ] **Terms of Service** — draft and publish; cover marketplace rules, prohibited use, liability limits, dispute resolution.
- [ ] **Privacy Policy** — GDPR/CCPA compliant; data collected, retention periods, user rights.
- [ ] **Stripe Connect compliance** — users accepting payouts must agree to Stripe Connected Account Agreement; gate onboard button behind a checkbox.
- [ ] **KYC/AML for payouts** — Stripe handles via Express onboarding; document the user-facing flow.
- [ ] **Tax reporting** — Stripe Connect handles 1099-K for US payees above threshold; document this for agents.
- [ ] **Prohibited use policy** — document what agents/callers cannot do (spam, fraud, CSAM) and how violations are handled.

---

## 12. Marketing & Growth (P1/P2)

- [ ] **Landing page copy** — clear headline, value prop, pricing, and CTA for agent builders and buyers.
- [ ] **Pricing page** — public breakdown of platform fee (10%), Stripe payout fee, no subscription cost.
- [ ] **Analytics** — integrate Plausible or GA4 for traffic and conversion tracking.
- [ ] **Social accounts** — register Twitter/X, LinkedIn handles for `agentmarket`.
- [ ] **Support channel** — set up support email (`support@...`) and/or Discord server.
- [ ] **Changelog** — running changelog of notable changes starting from current state.
- [ ] **Blog / launch post** — announce on Hacker News, Product Hunt, Twitter.

---

## 13. Documentation (P1)

- [ ] **API reference** — audit every endpoint in `server.py` against `docs/api-reference.md`; update stale entries.
- [x] ~~**MCP integration guide** — `docs/mcp-integration.md` covers Claude Code, Claude Desktop, env vars, A2A vs MCP choice.~~
- [ ] **A2A integration guide** — how to configure AgentMarket as a participant in Google A2A networks.
- [ ] **Dispute guide** — for callers: how to file, timeline. For agents: how to respond.
- [ ] **`output_examples` on landing page** — show 2–3 real input→output pairs for built-in agents on the homepage/agent list. Biggest single UX improvement for cold visitors deciding whether to fund a wallet. (Backend data is now populated for built-ins; frontend component still needed.)

---

## 14. Pre-launch Checklist

- [ ] All P0 items above complete
- [ ] Full test suite passing (0 failures)
- [ ] `pip-audit` and `npm audit` clean
- [ ] Domain registered, DNS live, SSL active
- [ ] Stripe live keys configured; live deposit + withdrawal tested end-to-end
- [ ] Stripe Connect platform profile complete
- [ ] Production Docker image runs as non-root with HEALTHCHECK
- [ ] Staging environment smoke-tested
- [ ] Sentry integrated and catching errors
- [ ] Uptime monitor on `/health`
- [ ] Transactional email tested (job complete, deposit, welcome)
- [ ] ToS and Privacy Policy published and linked in footer
- [ ] At least 5 working, benchmarked built-in agents with output examples
- [x] ~~`callback_url` end-to-end tested with HMAC verification~~
- [ ] Load test: 50 concurrent job creates + completes; no 500s, ledger invariant holds
