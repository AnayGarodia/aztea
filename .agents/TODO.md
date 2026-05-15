# Aztea — Open Work

> Source of truth for launch blockers and in-flight work. Update before ending a session.
> Operational reference: `docs/runbooks/`. Deep architecture: `CLAUDE.md`. Quick brief: `AGENTS.md`.

## Launch Blockers
<!-- Things that must ship before broader launch. Owner + target date required.
     Format: - [ ] (owner: ___, target: YYYY-MM-DD) <blocker> -->
_None at present._

## In Progress
<!-- Active work. One line per item: branch, what's left. -->
- [ ] **Step 1 Elixir activation** — code shipped (`bd58a2a`), `AZTEA_ELIXIR_EVENTS` flag still off in prod; needs Caddy `/elixir/socket` block + env vars + `mix deps.get` + restart aztea-elixir before flipping

## Done — recent
<!-- Last 5–10 shipped items with date and commit short sha. Trim aggressively. -->
- 2026-05-15 — Migration runner race fix: `_apply_migrations_postgres` now takes a session-level advisory lock (`MIGRATION_ADVISORY_LOCK_ID = 4297493287`, 60s timeout) so two uvicorn workers can't both apply the same pending migration. Closes the 2026-05-15 deploy-of-0046 incident where one worker died on `UniqueViolation`. SQLite path unchanged.
- 2026-05-15 — Pipeline discoverability: extended `GET /recipes` with `steps[]` + `estimated_total_cost_usd` + `missing_agents[]`; new `/workflows` frontend page with Run-workflow dialog; `manage_workflow(action="list_recipes")` MCP action inherits the same shape. Recon found `/recipes` already existed at `part_014.py:1183` so this is field-extension + UI rather than new routes. (commit `8a6e4fe`)
- 2026-05-15 — Reconciliation auto-repair: `?auto_repair=true` on `/ops/payments/reconcile` rewrites below-threshold `balance_cents` + `held_cents` drift in place; above-threshold drift still surfaced for human review; new `repair_wallet_held_cache` helper + `AUTO_REPAIR_THRESHOLD_CENTS` flag ($100 default, env-overridable) (commit `b0e696d`)
- 2026-05-15 — Reserve-hold pattern for agent payouts: `wallet_holds` table + `held_cents` cache + sweeper + Stripe withdrawal enforcement + dual-counter defense-in-depth; replaces silent-skip clawback (commit `9d9776e`)
- 2026-05-15 — Per-key sliding-window rate-limit middleware: 120 RPM caller / 600 worker / 60 anon / 10 RPS burst / LRU-bounded / fail-open (commit `73e97d4`)
- 2026-05-15 — Warm copy sweep: `frontend/src/utils/errorCopy.js` + `docs/voice.md` + 12 catch-site migrations; surfaces `retry_after_seconds` on 429 and `request_id` on 5xx (commit `97efdfa`)
- 2026-05-15 — SDK exception contracts + 8 Hypothesis property tests for `make_error` envelope shape; pinned `hypothesis>=6.100` already in `requirements-dev.txt` (commit `f8676fc`)
- 2026-05-15 — Step 1 strangle-fig migration: Phoenix.PubSub + Channels for realtime job events, feature-flagged off (commit `bd58a2a`)
- 2026-05-15 — Silent-failure sweep: payout-curve counter + 3 structured error envelopes + dispute/manifest/claim-token taxonomy codes + SDK hints (commit `f139a73`)
- 2026-05-15 — Observability upgrade: `job_duration_seconds` histogram + `builtin_agent_calls_total` counter + `GET /health` returning `{status, db, llm_providers, version}` (commit `476da23`)
- 2026-05-15 — TypeScript SDK parity: `AgentServer`, `poll_job_to_completion`, clarification handling (commit `53052b4`)
- 2026-05-15 — Co-pilot mode end-to-end integration tests (6 tests covering steer/progress/stop_when full flow) (commit `ff214a6`)
- 2026-05-15 — Federated reputation blend: hosted global trust auto-merged into `compute_trust_metrics()` with evidence-weighted blend (commit `2ef464d`)
- 2026-05-15 — Removed 15 dead built-in agents and the old `sdks/python/` SDK; `SUNSET_DEPRECATED_AGENT_IDS` now empty; curated count 35 → 29 (commit `ad14af3`)
- 2026-05-15 — Doc audit: 16 files fixed, 7 dead session artifacts deleted (commit `c20657a`)

## Backlog
<!-- Known gaps, not yet scheduled. -->
- [ ] **Postgres charge race-guard hardening.** `core/payments/base.py:18` notes phantom-read risk under READ COMMITTED. SQLite path uses `BEGIN IMMEDIATE` and is solid. Add a Postgres concurrency stress test before high-load prod traffic.
- [ ] **Worker disappearance reassign.** Today the lease times out and the caller is refunded rather than re-served. For built-in agents this is fine because the in-process worker pool is N-of-N. For third-party agents, decide whether a fallback retry to a different worker is in scope.
- [ ] **MCP tool count drift CI check.** Lazy mode advertises **9 tools** (`scripts/aztea_mcp_server.py`). Several docs previously said "four-tool surface" or "seven tools". Add a CI check or doctest that asserts the published tool list against the code so the next rename doesn't silently drift.
- [ ] Re-evaluate `core.listing_safety` ImportError fallback in `sdks/python-sdk/aztea/cli/publish.py:50` — kept for partial-install ergonomics; covered by `tests/test_cli_publish_safety_fallback.py`. Decide whether to make it a hard import once partial installs are no longer supported.
- [ ] Continue splitting any SDK / server module approaching the 1000-line CI hard limit (`scripts/check_file_line_budget.py`).

## Conventions
- Dates absolute (YYYY-MM-DD), never "Thursday" / "next week".
- Commit short sha for shipped items.
- Move items between sections rather than rewriting.
- Owner must be a person or `@team` handle; "TBD" is not an owner.
