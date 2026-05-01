# AGENTS.md — orienting any AI/contributor working on Aztea

This is the short, opinionated brief. The full contributor guide is `CLAUDE.md`. Read this first; read CLAUDE.md before you ship.

---

## What you are working on

Aztea is the **identity, payment, and dispute-resolution layer for agent-to-agent commerce**.

Two horizons:

- **Local goal — launch:** ship something individual developers and small teams *want to use today*. Buyer surface (Claude Code MCP, CLI, SDK, web) gets the bulk of polish in the launch window.
- **Global goal — north star:** open infrastructure where any agent on any platform can hire, pay, trust, and settle disputes with any other agent. Stripe + Upwork + Dun & Bradstreet, but the participants are software.

Every decision is graded against both. Plumbing that exists *only* for the global goal (DIDs, signed receipts, scoped agent caller keys, payout curves) is intentional, not over-engineering.

---

## Honest status

| What | State |
|---|---|
| Buyer adoption (the local goal) | ~75% — last external audit gave it a "yes for non-critical, not yet for production-critical" verdict |
| A2A commerce (the global goal) | ~25% — primitives exist, no production A2A jobs have run, no federation, no portable VCs |

Don't pretend either number is higher. See the table in `CLAUDE.md` ("Where we are vs. where we're going") for the full breakdown.

---

## Pre-launch priorities (current)

Ordered by impact / effort. Tick them off in this file when shipped — same PR.

1. ⏳ Eval-gate before community SKILL.md goes public (close the demo-skill pollution loop)
2. ⏳ Per-agent reputation dashboard (p50/p99 latency, success rate, refund rate, dispute rate) — backend has the data, page is bare today
3. ⏳ `git_diff_review` recipe — diff in, markdown review out (the killer demo)
4. ⏳ Output-shape templates (`as: "markdown" | "github_pr_comment" | "slack_blocks"`)
5. ⏳ Public status page + signed monetary-policy statement at `/policy/credits`
6. ⏳ Cold-install QA on Mac / Linux / Windows for `npx aztea-cli@latest init`
7. ⏳ Honest `ROADMAP.md` listing every global-goal item with a timeline
8. ⏳ A2A end-to-end demo using `azac_*` keys — one Aztea agent hires another in production
9. ⏳ Frontend tech-debt sweep: kill remaining inline-style blocks across DashboardPage / JobDetailPage / SettingsPage

Recently shipped (don't redo):

- ✅ Stripe Connect SDK + CLI: `client.get_connect_status / start_connect_onboarding / withdraw / list_withdrawals`; `aztea wallet connect / withdraw / withdrawals`. Backend + frontend already had the routes — sellers can now cash out through every surface.
- ✅ JobReceipt panel on JobDetailPage: in-browser Ed25519 verification of the signed job receipt via WebCrypto. The platform cannot forge a passing verification — the page does the cryptography. Frontend consumers of the moat now exist.
- ✅ `client.stream_job(job_id)` SDK helper — convenience wrapper around the existing `/jobs/{id}/stream` SSE endpoint.
- ✅ Wiring pass: `client.cancel_job/rate_job/dispute_job/get_dispute/retry_job/estimate_cost/list_jobs/get_agent_did/get_job_signature/verify_job/create_agent_caller_key/list_agent_caller_keys`; CLI `aztea jobs cancel/rate/dispute/verify/estimate`; MCP `aztea_data_retention_policy`, `aztea_verify_job`. Closes the "primitives built but not exposed" gap.
- ✅ `docs/identity-verification.md` cookbook published (the moat is now usable).
- ✅ `aztea_cancel_job` meta-tool + `POST /jobs/{id}/cancel` route (commit `dc5ea9a`, 2026-05-01).
- ✅ Privacy gate against `aztea_get_examples` leak — three-layer guard in `_record_public_work_example` (commit `dc5ea9a`).
- ✅ Demo-skill blocklist on `/registry/agents` (commit `6419fa6`).
- ✅ Word-boundary truncation in MCP search render (commit `dc5ea9a`).
- ✅ Search-ranker verb rules (sql_explainer vs db_sandbox by intent) (commit `dc5ea9a`).
- ✅ `slug` accepted as alias to `agent_id` on `aztea_estimate_cost` and `aztea_get_examples` (commit `dc5ea9a`).
- ✅ Required-field schemas on `cve_lookup` and `code_review` (commit `dc5ea9a`).
- ✅ Output-schema fields surfaced by `aztea_describe` (commit `dc5ea9a`).
- ✅ `recipe_id` canonical, `recipe_name` deprecated alias (commit `dc5ea9a`).

---

## Working agreement for AI sessions

You are most useful when you:

1. **Read `CLAUDE.md` end-to-end** before any non-trivial change. The non-negotiable engineering rules there are real: no float ledger, no raw `sqlite3.connect`, no migrations deleted, no force-push to main, no skipping pre-commit hooks, no toasts for errors.
2. **Match the existing style.** Small structured error envelopes (`{"error": {"code": ..., "message": ...}}`), 4-space Python, type hints, ES modules, PascalCase components, camelCase helpers, kebab-case CSS classes. Prefer narrow edits inside existing modules over new abstractions.
3. **Update the status table in CLAUDE.md and the priority list above** when you ship something that closes a roadmap gap. Same PR.
4. **Explain trade-offs in the commit message.** "Why this and not the alternative" matters more than "what changed."
5. **Test the full failure path.** Refunds must fire on agent failures. The session budget cap must hold under burst load. Deleting an agent must cascade. If you change a money path and don't add a test, your PR is incomplete.
6. **Run `make evals` before you push anything that touches an agent surface. Run `make smoke` against prod after deploy.**

Don't:

- Ship features that exist only because "it's a cool primitive." Tie every change to either the local or global goal.
- Add new third-party deps without a strong reason — the platform stays small on purpose.
- Touch the deprecated agent set (`github_fetcher`, `pr_reviewer`, `test_generator`, `spec_writer`, `changelog_agent`, `package_finder`) — they sunset 2026-07-26.

---

## Build, test, deploy

- `make dev` — backend with reload
- `make test-venv` — full backend suite (uses `.venv` if present)
- `make evals` — golden agent contract suite + launch alert evaluator
- `make smoke` — buyer-path harness against `$AZTEA_BASE_URL` (needs `AZTEA_API_KEY`)
- `make alerts` — operational alert evaluator (needs `AZTEA_API_KEY`)
- `make launch-check` — bundles evals + alerts as a launch gate
- `npm --prefix frontend run dev` / `build` / `test`
- `bash scripts/release_publish_local.sh` — push, publish changed packages, deploy prod. Use only when the tree is intentional and tested.

---

## Coding conventions (the non-obvious ones)

- **Python files stay < 1000 lines.** `scripts/check_file_line_budget.py` enforces this. Large modules become packages with `__init__.py` re-exports.
- **`server/application_parts/part_NNN.py`** are ordered shards. `part_000.py` owns all imports. New routes go in the shard that owns the concern. Keep each < 900 lines.
- **All outbound URLs through `core/url_security.py`** — including agent endpoints, webhook URLs, and git-clone paths. SSRF on a marketplace is reputational suicide.
- **`.text`, never `.content`** on `LLMResponse`. Using `.content` silently returns `None`.
- **Never pass `model=` to `CompletionRequest` when using `run_with_fallback`.** The chain selects the model.
- **Frontend errors must be inline, never toast-only.** Toasts are for success.

---

## Agents (the labor side)

Adding a built-in agent: full checklist in CLAUDE.md ("Adding a new built-in agent"). The bar:

> **Agents earn a place in the public marketplace by doing something Claude can't do in a chat session.** Real API data, live fetches, actual code execution — not LLM prompting with a nice schema.

If your new agent is "LLM with a system prompt and a JSON schema," it does not belong in the curated set. Six existing agents like that are sunsetting on 2026-07-26.

---

## Money, the part that has to be perfect

- Integer cents only.
- Insert-only ledger.
- Compensating entries, never UPDATE/DELETE on `transactions`.
- Pre-call charge + post-call payout/refund each have race guards. New settlement paths must replicate the guard.
- `wallets.balance_cents` is a cache; updates happen in the same SQL transaction as the ledger row.
- `POST /ops/payments/reconcile` must return zero drift. CI / cron treats any drift as a launch blocker.

If you are uncertain about a money path: open `core/payments/base.py`, read it, then ask. Don't guess.

---

## Trust & identity (the part that's the moat)

The DID + signing primitives exist. They work end-to-end at the platform level. **They are not yet exposed to buyers.** Anyone who ships a `client.verify_job(job_id)` helper, an `aztea_data_retention_policy(agent_id)` meta-tool, or a public docs page that explains how to verify a receipt is moving the local goal forward more than any new agent could.

If you are looking for high-leverage work: that's it.

---

## What good looks like

- A new contributor reads `CLAUDE.md` + this file, opens `make dev`, makes a change, runs `make test-venv` and `make evals`, ships a PR with screenshots and a tight commit message in under 90 minutes.
- An external buyer runs `npx -y aztea-cli@latest init`, gets $2 of free credit, asks Claude Code to "audit my requirements.txt for CVEs," gets a structured answer in under 5 seconds, and can verify the receipt was signed by the agent. **All of that works today** — except the verification step. Ship that.
- An agent on another platform calls one of your agents using its `azac_*` key, pays in stablecoin, settles a dispute through a published contract. **None of that works today.** That's the global goal.

Build with both audiences in your head.

---

## Repository structure (quick map)

- `server/application_parts/` — ordered shards that compose into `server.application` (one logical FastAPI app)
- `agents/` — built-in agent implementations (real-tool agents in the curated set)
- `core/` — auth, payments, jobs, registry, embeddings, migrations, identity, disputes
- `frontend/` — Vite/React app
- `sdks/` — Python SDK + TypeScript SDK + `sdks/aztea-cli` (npx install path)
- `tui/` — Textual-based terminal UI (`aztea-tui`)
- `migrations/` — SQL migrations applied once on startup, never deleted
- `scripts/` — release script, MCP server, smoke test, alerts evaluator
- `tests/` + `tests/integration/` — pytest backend, vitest frontend
- `docs/` — public docs + runbooks

Full structure with line counts, owner annotations, and the "what each shard owns" guide is in `CLAUDE.md`.
