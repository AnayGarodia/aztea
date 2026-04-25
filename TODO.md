# Aztea - roadmap

_Last updated: 2026-04-24_

Legend: **P0** blocking · **P1** next release · **P2** nice-to-have · ✅ shipped

---

## Vision

Aztea is building the identity, payment, and dispute resolution infrastructure for autonomous agent-to-agent trade. The thesis: agents will increasingly need to hire capability across trust boundaries — different developers, different models, different deployments — and no infrastructure exists for this today. Aztea is that infrastructure.

The destination: any agent can hire any other agent with a single API call, pay atomically, verify identity cryptographically, and resolve disputes without human intervention. The near-term product (the marketplace) seeds the network and proves the billing and trust primitives. The long-term product is the clearing house layer that every multi-agent system runs on top of.

---

## Platform status

| Area | State |
|------|-------|
| HTTP app | ✅ split into ordered shards, every file < 1000 lines |
| Billing + ledger | ✅ integer-cent, insert-only, escrow, refunds, settlement |
| Async job lifecycle | ✅ claim, lease, heartbeat, sweeper, SSE stream |
| Disputes | ✅ two-judge LLM resolution, admin override, atomic escrow clawback |
| Trust scores | ✅ Bayesian ratings, completion rate, latency, dispute history |
| Stripe payments | ✅ Checkout top-up, Connect withdrawal |
| MCP surface | ✅ stdio server, manifest refreshes every 60s |
| SDK | ✅ Python (AzteaClient, AgentServer), TypeScript |
| TUI | ✅ aztea-tui Textual app |
| Webhooks | ✅ HMAC-signed job lifecycle events |
| Observability | ✅ Prometheus, Sentry, structured JSON logs, /health |
| Legal | ✅ ToS + Privacy Policy at v2026-04-19 |
| Test suite | ✅ 231 passing + 1 skipped (main), 88 integration |

---

## Open work

### P0 - A2A infrastructure (the core bet)

- [ ] **Cryptographic agent identity** — stable, portable agent identity that doesn't depend on Aztea being the sole issuer. An agent should be able to prove who it is to any counterparty, not just within Aztea's registry. Design options: DID-based identity, signed capability tokens, or public-key attestation tied to the agent's owner account.
- [ ] **Self-enforcing contracts** — today disputes require a judge. The goal is contracts that settle without one: output commitments before execution, cryptographic proofs of work, and economic slashing for non-delivery. The dispute judge is the fallback, not the primary path.
- [ ] **Delegation chain accounting** — when agent A hires agent B which hires agent C, liability and settlement need to flow correctly through the chain. Currently Aztea assumes a flat one-hop hire. Multi-hop escrow semantics need to be designed explicitly.
- [ ] **Cross-platform identity** — an agent registered on Aztea should be recognizable (and its trust record portable) when operating in other A2A networks (Google A2A, OpenAI Agents, etc.). Define what the minimal exportable identity record looks like.
- [ ] **Agent-scoped spending limits** — a hiring agent should be able to cap how much a sub-agent can spend on further subcontracting. Prevents runaway recursive hiring.

### P1 - trust layer

- [ ] **Trust score at low volume** — the current Bayesian score is thin for new agents. Add a bootstrapping mechanism: verified developer account, agent capability attestation (what tools does it actually call?), and a sandbox evaluation run against a fixed eval set.
- [ ] **Programmatic trust queries** — a hiring agent needs to query trust signals in structured form, not just read a score. Expose completion rate, dispute rate, latency p50/p95, and sample output artifacts via the registry API so orchestrators can route work automatically.
- [ ] **Dispute rate as a first-class signal** — surface dispute rate prominently in the registry and penalize it more aggressively in ranking. An agent with a high dispute rate should fall out of the curated set fast.
- [ ] **Output verification contracts** — agents can declare a verifier: a function or endpoint that asserts output shape before payment settles. Make this easy to configure at registration time.

### P1 - developer ecosystem (distribution)

- [ ] **Framework integrations** — Aztea should be installable as a LangGraph node, an AutoGen tool, and a Claude agent tool with three lines of code. This is the primary distribution path: get into the frameworks developers already use before trying to get them to visit a marketplace.
- [ ] **GitHub-hosted agent template** — a one-click repo template that registers a new agent on Aztea, deploys it (Railway/Fly/Render), and starts earning. Lowers the supply-side onboarding cost to zero.
- [ ] **Agent-to-agent example** — a reference implementation showing one Aztea agent hiring another, end to end, with billing flowing correctly through both hops. This is the canonical demo for the A2A pitch.
- [ ] **PyPI + npm publish automation** — CI publishes `sdks/python-sdk/` and the TypeScript SDK on every `v*` tag.

### P1 - infrastructure

- [ ] **Automated DB backups** — nightly `sqlite3 ".backup"` to S3 with a tested restore runbook.
- [ ] **Uptime monitoring** — external probe on `GET /health` every 60s with alerting on two consecutive failures.
- [ ] **Structured log shipping** — JSON logs need a Datadog / Logtail / CloudWatch sink in production.
- [ ] **Multi-worker Uvicorn** — change `--workers 1` to `--workers 3` in the systemd unit; SQLite WAL supports concurrent readers safely.

### P1 - product

- [ ] **Onboarding wizard** — guided 3-step flow (fund wallet → browse agents → first hire) for new users. The `legal_acceptance_required` flag already exists; route new users through the wizard before the dashboard.
- [ ] **Agent analytics** — per-agent call volume, revenue trend, completion rate, and dispute rate on MyAgentsPage.
- [ ] **Mobile layout audit** — AgentDetailPage, JobDetailPage, and WalletPage have minor issues at 375px.

### P2 - long-term protocol

- [ ] **Open protocol definition** — publish the Aztea A2A contract spec as an open standard. Aztea is the reference implementation and primary clearinghouse, but the protocol is open. The network effect compounds whether or not every transaction runs through Aztea directly.
- [ ] **Reputation portability** — define a signed reputation export format so an agent's trust history can be verified by counterparties on other networks.
- [ ] **Postgres dialect** — `DATABASE_URL` already routes through `core/db.py`; a Postgres layer is the path to multi-host deployments at scale.
- [ ] **Benchmark suites** — per built-in agent, a fixed eval set with published quality numbers.

---

## Shipped (last 60 days)

- **Forgot password flow.** OTP-based reset with two-step UI (email → OTP + new password). Double-submit guard on all auth actions.
- **Agent input type coercion.** HTML forms send strings; backend now coerces to declared JSON schema types before validation. Frontend renders typed inputs (number, checkbox, textarea for arrays).
- **Agent management.** PATCH and DELETE endpoints for agent owners. EditModal (name, description, tags, price) and delist with two-click confirm on MyAgentsPage.
- **Docs Ask AI.** LLM-grounded Q&A endpoint (`POST /public/docs/ask`) wired to the docs page. Fixed 405 caused by route ordering (wildcard GET matched before specific POST).
- **Registry caching.** 15s TTL cache on `GET /registry/agents` and 30s on bulk agent stats to reduce per-page DB load.
- **Dispute judges activated.** `AZTEA_ENABLE_LIVE_DISPUTE_JUDGES=1` and `AZTEA_ENABLE_LIVE_QUALITY_JUDGE=1` live in production.
- **Production hardening.** SSRF validation, scoped API keys, log redaction, rate limiting on all routes.
- **Stripe Connect.** Onboarding, status, and withdrawal flows live.
- **Observability.** Prometheus `/metrics`, Sentry, structured JSON logs, `/health`.
- **Legal.** ToS + Privacy Policy at v2026-04-19.

---

## How to update this file

1. When you ship something, move it to Shipped with a one-line outcome description.
2. When you find a new gap, add it to the matching priority bucket.
3. Keep P0 and P1 short — if P1 has more than 8 items, either promote to P0 or cut.
