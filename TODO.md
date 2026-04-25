# Aztea - roadmap

_Last updated: 2026-04-25_

Legend: **P0** blocking · **P1** next release · **P2** nice-to-have · ✅ shipped

---

## Vision

Aztea is the payment and trust infrastructure for AI agent trade. The long-term destination: any agent hires any other agent with one API call — billing, identity, escrow, and dispute resolution handled automatically.

**Current beachhead (as of 2026-04-25):** OpenClaw skill builders. OpenClaw is an open-source personal AI assistant with 247k GitHub stars and a skill marketplace (ClawHub) with 13,000+ skills — all earning $0. Aztea becomes the revenue layer for that ecosystem: a skill builder uploads their SKILL.md, sets a price, and starts earning 90% of every call. No server required on their end. This beachhead proves the billing and trust primitives on real usage before expanding to the broader A2A market.

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

## Open work — OpenClaw beachhead

### P0 — Foundation (nothing works for skill builders without these)

- [ ] **Hosted skill runner** — The most critical unlock. Build an execution layer that accepts a SKILL.md upload, stores it, and exposes a stable Aztea-hosted HTTPS endpoint (`https://aztea.ai/skills/{id}/run`) that the skill builder never has to maintain. When called, the runner extracts the skill's name, description, instructions, and parameter schema from the SKILL.md, then executes the skill via Aztea's existing `run_with_fallback` LLM chain using the skill content as the system prompt. The output is returned in standard job format. This means a skill builder with no server, no DevOps, and no coding background can list a skill and earn revenue. **Why it matters:** Without this, every skill builder needs to deploy and maintain their own HTTP server — the friction kills adoption. With it, listing a skill on Aztea is as simple as listing on GitHub. **Complexity: Large.** New DB table (`hosted_skills`), two backend endpoints (`POST /skills/upload`, `POST /skills/{id}/run`), a SKILL.md parser, and the execution bridge to the LLM layer.

- [ ] **SKILL.md parser** — OpenClaw skills have their own SKILL.md format (name, description, instructions block, parameter definitions, examples). This is different from Aztea's agent.md. Build a parser that extracts: display name, description, system prompt / instructions, and input parameter schema. Map these to the Aztea agent registration schema. Handle gracefully if the SKILL.md is malformed (return clear errors, not 500s). Research actual ClawHub SKILL.md examples before implementing — the format may have evolved. **Why it matters:** The hosted skill runner can't work without this. The parser is also the onboarding UI's preview step. **Complexity: Medium.** Primarily parsing logic; build on top of the existing `core/onboarding.py` pattern.

- [ ] **Auto-approval for hosted skills** — Today all externally registered agents go to `pending_review`. That's appropriate for unknown HTTP endpoints (we can't see the code). But for hosted skills — where Aztea is the server — manual review is unnecessary gatekeeping. In `POST /registry/register`, if `endpoint_url` matches the pattern `https://aztea.ai/skills/{id}/run` and the owner matches the skill record, set `review_status = "approved"` immediately. Reserve `pending_review` for external endpoints only. **Why it matters:** A skill builder uploads, hits submit, and is live within seconds. Any delay here kills the "wow" moment. **Complexity: Small.** Condition on endpoint URL pattern in the registration handler.

- [ ] **Builder vs. hirer signup split** — Currently there is one registration flow and one onboarding wizard (fund wallet → browse agents → get API keys). Builders should never see a wallet top-up prompt — they're here to earn, not spend. Add a role selector at signup ("I want to hire agents" / "I want to sell my skill"). Store as a `user_role` field (`caller` | `builder` | `both`). Route builders through a builder-specific onboarding: upload SKILL.md → set price → connect Stripe for payouts → live. Route hirers through the existing wallet-first flow. **Why it matters:** Showing a "top up your wallet" screen to a skill builder signals this platform wasn't built for them. **Complexity: Medium.** Backend: one new `user_role` field on users table. Frontend: role selector in `AuthPanel.jsx`, conditional routing in `OnboardingWizard.jsx`, new builder wizard steps.

---

### P1 — Core builder experience (makes it compelling)

- [ ] **Skill upload wizard (builder onboarding UI)** — A dedicated multi-step flow for skill builders, separate from `RegisterAgentPage.jsx`. Steps: (1) Upload or paste SKILL.md → (2) Preview parsed name, description, and parameters → (3) Set price with guidance UI (see below) → (4) Connect Stripe for payouts → (5) Confirmation screen showing the live listing URL and a copyable "How to hire your skill" snippet. Each step validates before advancing. Step 2 should surface any parse errors with specific line numbers so the builder can fix their SKILL.md without guessing. **Why it matters:** `RegisterAgentPage.jsx` is built for developers who know what an endpoint URL is. Skill builders don't. This wizard makes the process legible to a non-technical user. **Complexity: Medium.** New page (`SkillUploadPage.jsx`), wizard state machine, wired to the new `/skills/upload` backend endpoint.

- [ ] **Homepage rewrite** — The current landing page speaks to infrastructure buyers ("hire agents", "billing", "escrow"). Rewrite it to speak directly to OpenClaw skill builders. Lead with: "Your OpenClaw skill should have revenue." The hero should show the three-step builder journey: upload SKILL.md → set price → start earning. Below the fold: an earnings calculator (calls/month × your price × 90% = your take-home), a "live on ClawHub" badge grid showing real listed skills, and social proof from early builders. The primary CTA is "List your skill" — not "Hire an agent." Keep a secondary section for hirers ("Looking to hire skills?") and a tertiary "For developers" section. **Why it matters:** Acquisition. If the homepage doesn't speak to the person you're trying to reach, they bounce in 10 seconds. **Complexity: Medium.** Frontend only; full `LandingPage.jsx` rewrite.

- [ ] **Pricing guidance UI** — Skill builders have never priced an API call. Build a helper component embedded in the skill upload wizard's price-setting step. It should show: the median and range of prices across current Aztea listings (fetch from registry stats), and an earnings calculator: "If N callers use your skill M times per month at $X, you earn $(N × M × X × 0.90)/month." Suggest a default price based on median for the skill's tag category. **Why it matters:** Pricing paralysis is a real blocker. An informed default and a visual earnings calculation lets builders commit. **Complexity: Small.** New component; backend needs a `GET /registry/stats/price-distribution` endpoint or surface it from existing registry data.

- [ ] **Builder earnings dashboard** — Reframe `WalletPage.jsx` for builders. When `user_role === "builder"`, the primary view should show: "Earnings this month" (not "balance"), call count and revenue for each listed skill, a simple chart of earnings over time, trust score trend, and Stripe Connect payout status. The current "add funds" / "spending overview" panels should be hidden or collapsed for pure builders. The language should feel like income (Stripe Dashboard, Gumroad) not accounting (bank statement). **Why it matters:** Builders need to feel like they're running a business, not reading a ledger. The first time they see money arriving, that's the retention hook. **Complexity: Medium.** Conditional rendering in `WalletPage.jsx` based on `user_role`; no new backend routes needed (agent-wallet endpoints already exist).

- [ ] **Stripe Connect payout onboarding — builder context** — The Stripe Connect onboarding flow already exists in `WalletPage.jsx` but it's buried. In the builder wizard (step 4) and in the builder earnings dashboard, surface it prominently: "Connect your bank account to receive payouts." Show the current onboarding status (not started / in progress / active) with a clear next-step CTA at each state. When `charges_enabled && payouts_enabled`, show a green "Payouts active" badge. **Why it matters:** Builders won't list if they don't believe they can get paid. The payout setup should feel as easy as Stripe's own onboarding. **Complexity: Small.** Frontend only; the `/connect/onboard` and `/connect/status` backend routes already exist.

---

### P1 — Hirer experience (demand side)

- [ ] **Increase free credit to $2, surface it prominently** — The current $1 welcome credit exists but is applied quietly. Raise it to $2 (enough to hire most skills 10–40 times). In the hirer onboarding wizard, make this the first thing they see: "You have $2.00 to spend — no card needed." Make the credit amount visible on the dashboard until it's spent. **Why it matters:** Hirers need to experience value before they trust Aztea with a credit card. $2 is enough for a real demo moment. **Complexity: Small.** Change the credit constant in the registration handler; update copy in `OnboardingWizard.jsx` and `WalletPage.jsx`.

- [ ] **"Hire this skill" experience on skill listings** — Agent detail pages (`AgentDetailPage.jsx`) are built for the async job API. For hosted skills, provide a simpler "try it now" panel: a plain text input, a submit button, and streamed output. No JSON schema, no job lifecycle details in the UI — just input → output. Show the price per call and remaining balance. This is the hirer's first hire experience. **Why it matters:** The first hire should feel as simple as sending a message. **Complexity: Medium.** New `SkillHirePanel.jsx` component wired to the sync call endpoint (`POST /registry/agents/{id}/call`), shown when `agent.type === "hosted_skill"`.

---

### P1 — Production fixes

- [ ] **Transactional email (SMTP wired in prod)** — `core/email.py` is fully implemented with 8 email templates but silently no-ops if `SMTP_HOST` is not set. Wire up an email provider (Resend, Postmark, or AWS SES — all support SMTP relay) in the production `.env`. The templates already cover: welcome, job complete, job failed, deposit confirmed, dispute opened/resolved, withdrawal processed. Add two builder-specific templates: (1) "Your skill is live" (sent on auto-approval), (2) "You received a payout" (sent on withdrawal). **Why it matters:** Email is the main re-engagement channel. A builder who gets an email saying "someone just paid you $0.15" is a retained builder. **Complexity: Small.** Prod config change + two new email templates.

- [ ] **Live dispute judges** — `AZTEA_ENABLE_LIVE_DISPUTE_JUDGES` and `AZTEA_ENABLE_LIVE_QUALITY_JUDGE` are marked shipped above — confirm these are actually set in the production `.env` and verify that judge invocations are appearing in logs. **Why it matters:** Disputes that sit unresolved erode trust on both sides. **Complexity: Small.** Verification + env var audit.

---

### P2 — Growth and retention

- [ ] **"Listed on Aztea" badge and link** — Give each skill builder a public listing URL (`https://aztea.ai/agents/{id}`) and a simple badge (SVG + markdown snippet) they can embed in their ClawHub skill page: "Hire this skill via Aztea." This is the primary distribution loop: skill builders link back to Aztea from ClawHub, which drives hirer traffic. **Why it matters:** Viral loop. Every ClawHub skill listing becomes an Aztea acquisition channel. **Complexity: Small.** Static SVG badge asset + copy snippet shown in builder dashboard.

- [ ] **Agent analytics on builder dashboard** — For each listed skill: total calls, revenue, completion rate, dispute rate, and average quality score. Surface on `MyAgentsPage.jsx` as a simple table with sparkline trend. The data is in the DB (transactions, jobs, ratings tables) but not exposed to builders today. **Why it matters:** Builders optimize what they can measure. Call data turns a passive income stream into an active product. **Complexity: Small–Medium.** New backend route `GET /registry/agents/{id}/stats` + frontend component.

- [ ] **ClawHub import (one-click listing)** — Let builders paste a ClawHub skill URL; Aztea fetches the SKILL.md from the public repo, pre-fills the upload wizard, and shows a diff of what was parsed. This removes the step of manually downloading and re-uploading. **Why it matters:** Reduces friction from ~5 minutes to ~30 seconds for builders already on ClawHub. **Complexity: Medium.** Backend: URL fetch + HTML parse to find the raw SKILL.md link (GitHub raw URL pattern). SSRF-validate the URL. Reuse the SKILL.md parser.

---

### P3 — Developer features (deemphasize, do not remove)

The following features are production-grade and the code must stay intact. Remove them from primary navigation and homepage framing; move them to a secondary "For developers" section. Direct URLs keep working.

- [ ] **Move MCP server docs to "For developers"** — `scripts/aztea_mcp_server.py` and its docs page stay. Remove MCP from the primary nav and homepage integration tracks. Add a "For developers →" link in the footer that leads to a page listing: MCP server, Python SDK, TypeScript SDK, TUI, webhook docs, async job lifecycle. **Why it matters:** MCP is a developer-facing integration surface. Leading with it confuses skill builders who are not pipeline engineers. **Complexity: Small.** Nav change + new developer landing page.

- [ ] **Move SDK docs to "For developers"** — Python SDK (`sdks/python-sdk/`) and TypeScript SDK (`sdks/typescript/`) docs and links should live under the developer section, not in primary onboarding. Remove from the onboarding wizard's "get your API keys" step for builder-role users. **Complexity: Small.**

- [ ] **Move TUI to "For developers"** — `aztea-tui` stays published on PyPI. Remove the TUI install instructions from the primary docs sidebar. Keep them accessible via direct URL and from the developer section. **Complexity: Small.**

- [ ] **Remove orchestrator / multi-agent framing from homepage and primary nav** — Language like "for developers building pipelines," "orchestrate multiple agents," and "A2A infrastructure" should not appear in the primary homepage copy or nav. It belongs in the developer section. The homepage should have one message: "Your skill should have revenue." **Complexity: Small.** Copy edit in `LandingPage.jsx` and `AppShell.jsx` nav.

---

### P2 — A2A infrastructure (parked, not cancelled)

These items from the previous roadmap remain valuable. They become the next phase after the OpenClaw beachhead is proven. No work should start on these until P0 and P1 are shipped and at least 20 skill builders are earning.

- [ ] **Cryptographic agent identity (DIDs + signed outputs)** — IN PROGRESS. Each agent gets a `did:web:aztea.ai:agents:<id>` and an Ed25519 keypair. Outputs are signed at completion. Verify endpoint allows any external party to verify outputs without trusting Aztea.
- [ ] **Capability attestation** — Sandbox-verify what an agent claims to do. New agents declare capabilities; Aztea runs a test job to confirm. Verified capabilities get a badge.
- [ ] **Output provenance records** — Every artifact carries a signed metadata record (who made it, under what job, what license). Enables a secondary market and royalty flows.
- [ ] **Self-enforcing contracts** — Output commitments before execution, cryptographic proofs of work, economic slashing for non-delivery. Dispute judge becomes the fallback, not the primary path.
- [ ] **Delegation chain spending limits** — `max_subhire_spend_cents` cap per job that flows down the delegation chain.
- [ ] **Staking** — Owner locks capital behind an agent. Publicly visible, slashed on lost disputes.
- [ ] **Framework integrations** — Aztea as a LangGraph node, AutoGen tool, Claude agent tool.
- [ ] **GitHub-hosted agent template** — One-click repo that registers, deploys, and earns.
- [ ] **Open protocol definition** — Publish the A2A contract spec as an open standard.
- [ ] **PyPI + npm publish automation** — CI publishes SDKs on every `v*` tag.

---

## Infrastructure backlog (evergreen)

- [ ] **Automated DB backups** — Nightly `sqlite3 ".backup"` to S3 with a tested restore runbook.
- [ ] **Uptime monitoring** — External probe on `GET /health` every 60s with alerting.
- [ ] **Structured log shipping** — JSON logs need a Datadog / Logtail / CloudWatch sink in production.
- [ ] **Multi-worker Uvicorn** — Change `--workers 1` to `--workers 3` in the systemd unit.
- [ ] **Mobile layout audit** — `AgentDetailPage`, `JobDetailPage`, and `WalletPage` have minor issues at 375px.

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

## Execution order (recommended)

Start with P0 items in sequence — the hosted skill runner depends on the SKILL.md parser, and auto-approval depends on the runner existing. The builder/hirer signup split can be built in parallel with the runner. Once P0 is done, P1 items can be parallelized across frontend (homepage, builder dashboard, hire UX) and backend (earnings stats route, email templates).

```
Week 1:  SKILL.md parser → Hosted skill runner → Auto-approval
Week 2:  Builder/hirer signup split → Skill upload wizard
Week 3:  Homepage rewrite + Pricing guidance UI
Week 4:  Builder earnings dashboard + Stripe Connect builder context
Week 5:  Hirer free credit bump + "Hire this skill" panel + Email config
Week 6:  Developer section reorganization + Badge/link feature
```

---

## How to update this file

1. When you ship something, move it to Shipped with a one-line outcome description.
2. When you find a new gap, add it to the matching priority bucket.
3. Keep P0 short — if it has more than 5 items, something has gone wrong with scoping.
