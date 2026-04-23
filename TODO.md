# Aztea — current roadmap

_Last updated: 2026-04-23_

This is a living document. It tracks what is currently shipped, what is in
progress, and what is known to still be missing. Older launch-era items that
are now in production have been archived into the "Shipped" sections rather
than kept as noisy checkboxes.

Legend: **P0** launch blocker · **P1** next release · **P2** nice-to-have · ✅ shipped.

---

## Platform status at a glance

| Area | State | Source of truth |
|------|-------|-----------------|
| HTTP app | ✅ split into `server/application.py` + `application_parts/` (every file < 1000 lines) | `scripts/check_file_line_budget.py` |
| Built-in agent specs | ✅ split into `server/builtin_agents/specs_part1.py` + `specs_part2.py` | `server/builtin_agents/specs.py` |
| Split packages | ✅ `core.auth`, `core.jobs`, `core.payments`, `core.registry`, `core.models` | Each `__init__.py` re-exports its submodules |
| `/api/*` compat shim | ✅ middleware rewrites `/api/<path>` → `/<path>` | `server/application_parts/part_001.py::api_prefix_compat` |
| SPA fallback | ✅ serves `frontend/dist/index.html` + static assets | `server/application_parts/part_012.py::spa_root`, `spa_fallback` |
| Error envelope | ✅ `{error, message, details, request_id}` on every 4xx/5xx | `server/error_handlers.py` |
| Client input guards | ✅ payload size/depth/URL/price enforced UI-side too | `frontend/src/utils/inputGuards.js` |
| Test suite | ✅ 231 passing + 1 skipped (main); 88 passing (integration); SDK contract run separately | `pytest tests --ignore=tests/test_sdk_contract.py` |
| CI | ✅ `flake8` + pytest + frontend build on every push | `.github/workflows/ci.yml` |
| Legal | ✅ ToS + Privacy Policy published at v2026-04-19 | `docs/terms-of-service.md`, `docs/privacy-policy.md` |

---

## Open work

### P1 — infrastructure and reliability

- [ ] **Postgres readiness** — `DATABASE_URL` already routes through `core/db.py`; a Postgres dialect layer is still the long-term plan for multi-host deployments. Until then SQLite WAL + daily backup is sufficient.
- [ ] **Automated DB backups** — nightly `sqlite3 ".backup"` to S3 with a tested restore runbook. The current prod EC2 host does not have this cron installed.
- [ ] **Uptime monitoring** — external probe hitting `GET /health` every 60s with alerting on two consecutive failures.
- [ ] **Structured log shipping** — JSON logs are emitted via `core.logging_utils`; still need a Datadog / Logtail / CloudWatch sink in production.
- [ ] **Connection-pool tuning** — `DB_MAX_CONNECTIONS=32` is the current cap; add a queue-timeout guard so bursty traffic does not starve background workers.

### P1 — product

- [ ] **Onboarding wizard** — a 3-step flow (fund wallet, browse agents, first hire) for cold users. Logged-in state is tracked server-side (`legal_acceptance_required` / first-run flag already exist); the UI still needs to route new users through a guided experience instead of dropping them on the dashboard.
- [ ] **Mobile polish** — pages work at 375px but have minor layout quirks; audit `AgentDetailPage`, `JobDetailPage`, and `WalletPage`.
- [ ] **User notification preferences** — currently every transactional email fires; add an opt-out surface in Settings.
- [ ] **Agent analytics dashboard** — agent owners want per-agent call volume, revenue, and dispute rate on `MyAgentsPage`.
- [ ] **A2A integration guide** — a separate doc next to `docs/mcp-integration.md` covering how Aztea participates in Google A2A and other agent-to-agent networks.

### P1 — security and compliance

- [ ] **Dependency audit cadence** — schedule monthly `pip-audit` + `npm audit`; currently only manual.
- [ ] **Secrets rotation runbook** — document how to rotate `API_KEY`, Stripe keys, Groq/OpenAI/Anthropic keys, and PyPI token. (A reminder: the `.env` leaked during a support session on 2026-04-23 should already have been rotated end-to-end.)

### P2 — developer experience

- [ ] **PyPI publish automation** — currently the SDK has to be tagged + pushed manually. Add a GitHub Actions job that publishes `sdks/python-sdk/` on every `v*` tag using the stored `PYPI_TOKEN`.
- [ ] **npm publish automation** — same pattern for the TypeScript SDK.
- [ ] **Frontend test coverage** — Vitest is not wired up yet; add smoke tests for the critical user flows (register, hire, wallet top-up).
- [ ] **Pre-commit hook** — run `flake8` + `check_file_line_budget.py` before commit to avoid CI round-trips.

### P2 — agent quality

- [ ] **Benchmark suites** — per built-in agent, a fixed eval set that produces comparable quality numbers surfaced in the marketplace.
- [ ] **More built-ins with real tool use** — the curated public set favours agents that do external work. Good candidates: translation via a real API, OCR via a real API, data-analyst backed by DuckDB.

---

## Shipped (selected — last 60 days)

These are not exhaustive, but capture the headline work already in production:

- **Modular refactor.** Every file under `server/` and `core/` is under 1000 lines. Splits preserve the legacy single-module namespaces so monkeypatches keep working.
- **Production hardening pass.** Strict client-side guardrails for agent registration and invoke payloads, actionable error messages, `/api/*` compat middleware, SPA fallback route.
- **Split integration tests.** `tests/test_server_api_integration.py` became `tests/integration/` with shared helpers and per-domain files.
- **CI cleanup.** Flake8 per-file ignores for legitimately shared namespaces; dead duplicate job helpers removed.
- **Stripe Connect.** Onboarding, status, and withdrawal flows live behind feature flags and are covered by integration tests.
- **Disputes and judgments.** Two-judge AI resolution, admin override, dispute-deposit escrow, dispute-window settlement holds.
- **Observability.** Prometheus `/metrics`, Sentry integration, structured JSON logs, `/health` with DB / disk / memory probes.
- **Legal.** Terms of Service and Privacy Policy published at version `2026-04-19` and enforced via `legal_acceptance_required` in auth responses.
- **Frontend aesthetic pass.** Distinctive typography, motion-based reveals, `ModelBadge`, provider filters, skeleton/toast/empty states on every list and detail page.

---

## How to update this file

1. When you ship something that was on this list, move it to "Shipped (selected)" with a short sentence describing the outcome.
2. When you find a new gap, add it to the matching priority bucket with a short actionable description.
3. Keep the priority buckets short — if P1 has more than ~8 items, promote the most urgent to P0 or split into a dedicated planning doc.
