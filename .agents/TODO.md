# Aztea — Open Work

> Source of truth for launch blockers and in-flight work. Update before ending a session.
> Operational reference: `docs/runbooks/`. Deep architecture: `CLAUDE.md`. Quick brief: `AGENTS.md`.

## Launch Blockers
<!-- Things that must ship before broader launch. Owner + target date required.
     Format: - [ ] (owner: ___, target: YYYY-MM-DD) <blocker> -->
- [ ] (owner: ___, target: ___) Re-anchor `CLAUDE.md` "Current test status" line with a fresh end-to-end run; numbers there are dated 2026-05-07.
- [ ] (owner: ___, target: ___) Pin `hypothesis` in `requirements-dev.txt`. `tests/property/` and `tests/test_listing_safety_fuzz_v2.py` currently fail collection on a fresh dev install. Until pinned, the canonical pytest command must `--ignore` both paths (already updated in `CLAUDE.md`).
- [ ] (owner: ___, target: ___) Co-pilot mode (PR #14) needs end-to-end steer-mid-job execution tests. `tests/test_copilot_mode.py` only covers `stop_when` validation today. Receipts + lease side-effects + steer-message routing are wired; what's missing is a test that drives a job through `steer → resume → terminal_state` and asserts on the signed transcript.

## In Progress
<!-- Active work. One line per item: branch, what's left. -->
- [ ] (placeholder)

## Done — recent
<!-- Last 5–10 shipped items with date and commit short sha. Trim aggressively. -->
- 2026-05-10 — full doc passthrough to align README / AGENTS.md / CLAUDE.md / oss-vs-hosted / reputation / mcp-integration with code reality (agent count, MCP tool surface, dual-backend persistence, federation status, honest-gaps list); commit ___
- 2026-05-09 — created `.agents/TODO.md`, added `aztea publish` wizard tests, split `sdks/python-sdk/aztea/client.py` into a subpackage, added `agents/ai_red_teamer.py` tests (commit ___)

## Backlog
<!-- Known gaps, not yet scheduled. -->
- [ ] **Postgres charge race-guard hardening.** `core/payments/base.py:18` notes phantom-read risk under READ COMMITTED. SQLite path uses `BEGIN IMMEDIATE` and is solid. Add a Postgres concurrency stress test before high-load prod traffic.
- [ ] **Payout-curve clawback failure path** (`core/payout_curve.py:227-247`): if the agent wallet can't absorb the clawback (already withdrawn), the clawback is logged-and-skipped silently. Caller is not made whole. Decide between (a) operator alert, (b) a reserve-hold pattern that prevents clawback-time underwater agents, or (c) escalating to a system credit from the platform wallet.
- [ ] **Worker disappearance reassign.** Today the lease times out and the caller is refunded rather than re-served. For built-in agents this is fine because the in-process worker pool is N-of-N. For third-party agents, decide whether a fallback retry to a different worker is in scope.
- [ ] **Reconciliation auto-repair.** `POST /ops/payments/reconcile` reports drift; `repair_wallet_balance_cache()` is a manual second call. Either wire an `?auto_repair=1` flag (reviewer note: insert-only ledger discipline still applies) or document the manual playbook in `docs/runbooks/ledger-drift.md` more explicitly.
- [ ] **Federated reputation auto-merge.** `POST /jobs/{id}/rating` already exports anonymized ratings to hosted aztea.ai, and `GET /registry/agents/{id}/global-trust` reads the cross-instance score back on demand (501 in OSS). What's missing: blending the global score into the local `compute_trust_metrics()` output so search/auto-hire benefits from cross-instance signal automatically. Decide on the blend weight + staleness behaviour first.
- [ ] **TypeScript SDK polling/clarification.** `sdks/typescript/` exposes the API surface but lacks `poll_job_to_completion`, clarification handling, and an `AgentServer` equivalent. Caller-side only today. Bring it to parity with the Python SDK.
- [ ] **MCP tool count drift.** Lazy mode advertises **9 tools** (`scripts/aztea_mcp_server.py:1134-1142` — 4 lazy + 2 copilot + 3 grouped). Several docs that previously said "four-tool surface" or "seven tools" have been corrected; add a CI check or doctest that asserts the published tool list against the code so the next rename doesn't silently drift.
- [ ] Re-evaluate `core.listing_safety` ImportError fallback in `sdks/python-sdk/aztea/cli/publish.py:50` — kept for partial-install ergonomics; covered by `tests/test_cli_publish_safety_fallback.py`. Decide whether to make it a hard import once partial installs are no longer supported.
- [ ] Continue splitting any SDK / server module approaching the 1000-line CI hard limit (`scripts/check_file_line_budget.py`).

## Conventions
- Dates absolute (YYYY-MM-DD), never "Thursday" / "next week".
- Commit short sha for shipped items.
- Move items between sections rather than rewriting.
- Owner must be a person or `@team` handle; "TBD" is not an owner.
