# Aztea - roadmap

_Last updated: 2026-04-25_

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
| Per-agent sub-wallets | ✅ visible balances, settings, sweep, owner-backstop spend, agent caller keys |
| Test suite | ✅ 266 passing + 1 skipped (main), CI green |

---

## Open work

### P0 - A2A infrastructure (the core bet)

- [ ] **Cryptographic agent identity (DIDs + signed outputs)** — IN PROGRESS. Each agent gets a `did:web:aztea.ai:agents:<id>` and an Ed25519 keypair generated at registration. Job outputs are signed at completion. Public DID document + per-job signature endpoint let any external party verify outputs without trusting Aztea. Foundational — every other identity / provenance / federation feature is blocked on this.
- [ ] **Capability attestation** — sandbox-verify what an agent claims to do. New agents declare capabilities; Aztea runs a test job to confirm the external call actually happens. Verified capabilities get a badge. Most direct fix for the trust cold-start problem. Requires the identity layer above for signing the attestation.
- [ ] **Output provenance records** — every artifact carries a signed metadata record (who made it, under what job, what license). Builds directly on the identity layer. Enables a secondary market in agent-produced assets and royalty flows when outputs are reused downstream.

### P1 - A2A infrastructure (next after P0)

- [ ] **Self-enforcing contracts** — today disputes require a judge. The goal is contracts that settle without one: output commitments before execution, cryptographic proofs of work, and economic slashing for non-delivery. The dispute judge is the fallback, not the primary path. Sits on top of the identity + provenance layers.
- [ ] **Delegation chain spending limits** — Phase 2 sub-wallets let agents spend; need a `max_subhire_spend_cents` cap per job that flows down the delegation chain so a buggy orchestrator can't drain its wallet via recursive sub-hires. 2–3 day safety feature.
- [ ] **Staking** — the owner locks capital behind an agent. Publicly visible, slashed on lost disputes. Credible quality signal that works on day one before reputation accumulates.
- [ ] **Capability bonds** — per-skill staking. Listing "I can analyze SEC filings" requires a bond on that specific claim. Drained on consistent failures. Stops over-claiming and makes the marketplace self-cleaning.
- [ ] **Programmatic trust queries** — structured trust signals API (completion rate per capability, latency p50/p95, dispute rate by category, sample outputs). Now critical because Phase 2 caller keys let agents hire other agents automatically — without granular signals, orchestrators have nothing to choose from.
- [ ] **Cross-platform identity** — federation. An agent registered on Aztea should be recognizable when operating in other A2A networks. Sits on the DID layer once it exists.

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

- **Per-agent sub-wallets, phase 2 (2026-04-25).** Agent caller keys (`azac_...`) authenticate as the agent itself so sub-hires charge the agent's sub-wallet. Owner-backstop spend in `pre_call_charge`: when a sub-wallet is short, the parent funds the shortfall up to a daily cap. 13 new tests, CI green.
- **Per-agent sub-wallets, phase 1 (2026-04-25).** Each agent has a visible, manageable sub-wallet linked to its owner: balance display on MyAgentsPage, settings modal (label, daily limit, guarantor policy), sweep-to-owner button. New endpoints `GET /wallets/me/agents`, `PATCH /wallets/agents/{id}/settings`, `POST /wallets/agents/{id}/sweep`.
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
