# Critical invariants — never violate these

> Resolved reference for `CLAUDE.md`. The terse bright-line summary lives in `CLAUDE.md` → "Non-negotiables"; **read the relevant section here in full before touching money, the DB, auth, the MCP surface, observability, self-improving skills, or workspaces.**

## Money

- **Integer cents only.** Never store or pass floats for money. `price_per_call_usd` in specs is float for display only; the ledger always uses `*_cents INTEGER`.
- **Insert-only ledger.** `transactions` gets only INSERT, never UPDATE or DELETE. Corrections are compensating entries.
- **Double-settlement guard.** `pre_call_charge`, `post_call_payout`, and `post_call_refund` each have race guards (rowcount checks on wallet UPDATE). Every new settlement path must replicate the guard.
- **Dispute atomicity.** Dispute insert + escrow clawback MUST happen in one DB transaction. Lock failure rolls back the dispute row — see `core/disputes.py`.
- **Payout-curve clawbacks** use `charge`/`refund` ledger types only — never custom transaction types. Idempotency key: `payout_curve:{job_id}`. See `core/payout_curve.py`.
- **`wallets.balance_cents` is a cache.** It must be updated in the same SQL transaction as the ledger row that changes it. Validated by reconciliation runs (`POST /ops/payments/reconcile`).

## Database

- **Single connection manager.** All modules use `core/db.py`. Never open a raw `sqlite3.connect()` or `psycopg2.connect()` anywhere. The module exports `IntegrityError` / `OperationalError` / `ProgrammingError` so callers stay backend-agnostic.
- **Backend selection.** `DATABASE_URL=postgresql://...` selects Postgres (prod). Anything else (or unset) falls back to SQLite (dev/tests/CI). All SQL is written with `%s` placeholders; `core/db.py` rewrites them to `?` for SQLite. Tests must pass on both backends.
- **Thread-local pool, network I/O between transactions.** `DB_MAX_CONNECTIONS` (default 32) caps connections. Never hold a write lock during an HTTP call. SQLite gets WAL mode automatically; Postgres uses default isolation.
- **`caller_ratings` lives only in `reputation.py`.** `disputes.py` does not declare it. Do not re-declare or migrate this table elsewhere.
- **Migrations are idempotent.** Each `.sql` file is applied once via a `schema_migrations` table. Never re-use a migration filename; always add a new one.

## Auth & security

- **Scoped keys:** `caller`, `worker`, `admin`, plus agent-scoped worker keys (`azac_...`). Every mutation route checks scope and ownership.
- **API key values are never logged.** Log only the prefix (`az_xxx...`). Automatic redaction is in `logging_utils.py`.
- **All outbound URLs go through `url_security.py`** (agent endpoints, verifiers, webhooks, onboarding URLs, git clone paths). Private IPs, loopback, IPv6, and URL-encoded bypass chars are blocked. Dev override: `ALLOW_PRIVATE_OUTBOUND_URLS=1`.

## Privacy / work-example recording

- **Sensitive agents must never replay caller inputs.** `_record_public_work_example()` in `server/application_parts/part_003.py` drops on five independent gates: (a) hardcoded `_SENSITIVE_EXAMPLE_AGENT_IDS`, (b) the `examples_sensitive: True` flag on the spec, (c) the `Security` category, (d) the agent self-declared `pii_safe: True` (caller inputs likely contain PII), (e) the agent self-declared `outputs_not_stored: True` (publisher promised not to retain). Per-field redaction via `_redact_sensitive_for_example` also runs unconditionally before persistence as a defence-in-depth layer. New scanner / credential / PII-handling agents must set `examples_sensitive: True` and the Security category.

## Routing

- **FastAPI swagger lives under `/api/docs`**, not `/docs`. The SPA owns `/docs`. `app = FastAPI(docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json")` is enforced in `part_001.py`. Any new public-facing path must not collide with FastAPI's defaults.
- **Add new SPA-only paths to `_SPA_API_PREFIXES` only if they should 404 as JSON**. Otherwise FastAPI's catch-all will serve `index.html` and React Router resolves them.

## LLM layer

- **`LLMResponse.text` — not `.content`.** Every agent module must use `raw.text`. Using `.content` silently returns `None` at runtime.
- **Never pass `model=` to `CompletionRequest` when using `run_with_fallback`.** The fallback chain selects the model. Pass `model=""` or omit it.
- **Provider-agnostic.** Don't hardcode a provider or model in any built-in agent. Use `run_with_fallback(req)` which tries `AZTEA_LLM_DEFAULT_CHAIN` (env-overridable).
- **Graceful LLM degradation.** If synthesis fails because no LLM provider is configured, agents that performed real retrieval must still return the retrieval output rather than raising. See `agents/arxiv_research.py` for the pattern.

## OSS / hosted boundary

The codebase is **Apache-2.0** open source. It runs fully self-contained when `AZTEA_HOSTED_API_URL` is unset. Hosted aztea.ai layers a few paid services on top: dispute judges (LLM credits), hosted built-in agents, public registry syndication, federated reputation, and Stripe Connect.

- **The OSS build must never make a network call to `aztea.ai`.** The only module that talks to the hosted API is `core/hosted_client.py`. If you find yourself adding `requests.post("https://api.aztea.ai/...")` outside that file, stop — route it through `HostedClient` instead.
- **Every hosted call soft-fails to local.** A hosted outage degrades to local LLM (judges, prefer_hosted agents) or to deterministic fallback. A hosted call failure must never bubble up as a 500 to the caller.
- **OSS-mode tests live in `tests/test_oss_mode_isolation.py`.** They monkeypatch `requests.post` to raise so any accidental outbound call surfaces as a test failure. Add new assertions there when you wire a new hosted service.
- **Hosted-only routes return 501 with a structured pointer** (`docs/oss-vs-hosted.md`) to the hosted aztea.ai service. Use the pattern from `_stripe_unavailable_error` in `part_013.py`.
- **No hardcoded `aztea.ai` URLs in `core/`, `server/`, or `agents/`.** All public-facing URLs in code must read from `SERVER_BASE_URL` or `PUBLIC_BASE_URL` (email links). The 501 pointer copy is the only documented exception.
- **`PREFER_HOSTED_AGENT_IDS` in `server/builtin_agents/constants.py`** governs which built-ins try the hosted endpoint first. Default is local; opt agents in only when the hosted version meaningfully outperforms a self-hosted run with user-provided LLM keys.

See `docs/oss-vs-hosted.md` for the full local-vs-hosted matrix.

## Built-in agents

- Agent IDs are **deterministic UUID v5** from namespace `6ba7b810-9dad-11d1-80b4-00c04fd430c8` + `aztea.builtin.{slug}`. Constants live in `server/builtin_agents/constants.py` (single source of truth).
- **Only agents that demonstrate a platform primitive go in `CURATED_PUBLIC_BUILTIN_AGENT_IDS`.** The bar is stricter than "real tool" — each curated agent must show off subprocess isolation, live external data, or a specialist headless runtime that third-party builders will want to build on top of. Current count: **11 curated public agents** (10 from the 2026-05-26 platform-pivot cull — which dropped 17 more agents on top of the 2026-05-20 catalog-quality cull's 11 — plus `site_navigator`, the agent-readable-web magnet added 2026-06-01; see `SUNSET_DEPRECATED_AGENT_IDS` for the full list and per-agent reasoning). `SUNSET_DEPRECATED_AGENT_IDS` now holds **29 entries**. Internal endpoints stay wired so old job IDs and signed receipts continue to resolve; sunset agents remain hireable by direct slug / agent_id. Do not add LLM-only agents.
- Each new built-in agent needs: module in `agents/`, entry in `BUILTIN_INTERNAL_ENDPOINTS`, spec in `specs_part1.py` or `specs_part2.py`, case in `_execute_builtin_agent()`, and a structured error envelope.
- **Work examples** are stored via `_record_public_work_example()`. Pass `private_task=True` to skip recording. Ring buffer capped at `_AGENT_WORK_EXAMPLES_MAX`.

## MCP surface

- Tool names are plain `snake_case` from the agent name — no prefix.
- All manifest keys use `snake_case` (`input_schema`, `output_schema`, `price_per_call_usd`).
- `/mcp/invoke` authenticates via `auth.verify_agent_api_key` or a caller-scoped user key.
- The stdio MCP server (`sdks/python-sdk/aztea/mcp/server.py`, also reachable via the `scripts/aztea_mcp_server.py` compat shim) refreshes tools every 60s via the HTTP registry.
- **Lazy tool surface is ten tools** (Wave 2 verb-first names; both pre-Wave-2 `aztea_*` and Wave-2-legacy `*_specialist*` names dispatch via aliases): `search_agents`, `describe_agent`, `call_agent`, **`auto_call_agent`** (auto-invoke fast path), three grouped resource dispatchers `manage_job`, `manage_budget`, `manage_workflow`, and three observability tools — `aztea_status` (digest), `aztea_inspect` (entity drill-down), `aztea_query` (pre-canned views). The observability tools require admin scope on the configured key. `aztea_call_streaming` and `aztea_steer` were dropped 2026-05-17 — the 2026-05-17 extensive test report showed RECEIPT_NOT_BUILT (HTTP 425), 12 duplicated "started" partials, and `stop_when` never evaluating real partials. Refunds were honest but UX was misleading. Dispatch still recognises the names and returns `tool_not_supported`; the implementation in `sdks/python-sdk/aztea/mcp/copilot_tools.py` is retained for a future rewrite. The backend mechanics (`/jobs` with `stop_when_predicates`, `/jobs/{id}/messages` with `msg_type="steer"`) still work and are reachable through `manage_job` action verbs. `auto_call_agent` picks the best agent for an intent and runs it under hard cost/confidence/quality gates. All gates live in the backend at `POST /registry/agents/auto-hire` (`server/application_parts/part_012.py`); both MCP server frontends are thin proxies. Decision logic lives in `core/registry/auto_hire.py`; thresholds are env-tunable via `AZTEA_AUTO_INVOKE_*` flags. Alias map: `sdks/python-sdk/aztea/mcp/server.py:_LAZY_TOOL_NAME_ALIASES`.
- **Output formats**: Both `call_agent` and `auto_call_agent` accept `output_format` (`json | markdown | github_pr_comment | slack_blocks | text`). Renderer at `core/output_formats.py` dispatches by sniffing well-known output shapes (CodeReview, Linter, TypeChecker, DepAuditor, GitDiffAnalyzer, pipeline) — NOT by `agent_id` — so external agents inherit pretty rendering for free. Renderers must never raise; unknown shapes fall back to a generic JSON code-fence. The canonical `output` dict is left intact; the rendered string lands under `rendered_output` + `rendered_output_format`. Hooked in via `_decorate_with_rendered_output` in `part_008.py`.
- **Built-in recipes** (`core/recipes.py`): current curated recipes are `audit-deps` and `domain-health`. The 2026-05-26 platform-pivot cull removed `secret-scan-and-audit` and `security-audit-sealed` (both fanned out to the now-sunset `secret_scanner` agent); `ensure_builtin_recipes` tombstones stale rows so old API listings stop showing them. Recipes are useful workflow primitives, but they are not the product story. Do not frame Aztea around a single code-review demo; frame it as the trust, payment, identity, and recourse layer that lets agents hire specialist agents.

## Observability surface

Admin-scope-gated read-only API for "what is Aztea doing?" questions. Backs the three MCP tools `aztea_status` / `aztea_inspect` / `aztea_query` and is intended for direct ad-hoc queries by operators (and by Claude Code when the configured key has admin scope).

- **`GET /admin/usage/digest?window=24h|7d|30d`** — high-level rollup with trend deltas vs the prior-equal window. Returns `{calls, spend, top_agents, failing_agents, users, auto_hire}`. Example: `aztea_status(window='7d')`.
- **`GET /admin/usage/inspect?entity=<entity>&id=<id>`** — single-row drill-down. `entity ∈ {agent, user, job, decision}`. Example: `aztea_inspect(entity='job', id='abc-123')`.
- **`GET /admin/usage/query?view=<view>&window=&limit=&sort=`** — pre-canned list views. `view ∈ {no_match, failures, agent_health, user_activity, top_agents, dormant_users, spend_by_user, spend_by_agent, latency_outliers, recent_decisions}`. Unknown entities/views return 400 — silent empty fallbacks were rejected during design. Example: `aztea_query(view='no_match', window='30d', limit=10)`.

Source: `server/routes/admin_usage.py` (factory `create_router(...)` wired at the end of `part_011.py`). Inline SQL by design; extract to `core/usage_queries.py` only when a query is reused.

**Auto-hire decision persistence.** Every call to `POST /registry/agents/auto-hire` writes one row to `auto_hire_decisions` (migration 0047). Columns: `decision_id`, `caller_owner_id`, `caller_key_id`, `intent_text`, `intent_hash` (SHA-256), `auto_invoked` (0/1), `dry_run` (0/1), `reason` (gating outcome — `no_match`, `insufficient_confidence`, `price_exceeded`, etc.), `chosen_agent_id`, `confidence`, `candidates_json` (top-N snapshot), `resulting_job_id`. The write is fire-and-forget — failures never block the response. See `core/registry/decision_audit.py`.

**`jobs.origin` taxonomy.** `origin ∈ {direct, auto_hire, pipeline, compare, recipe, watcher}` (migration 0049). Validation lives in `core/jobs/crud.py::_validate_origin`; the allowed-set is the single source of truth. The auto-hire delegation path uses a `ContextVar` (`core/registry/origin_context.py::use_origin`) so the standard `registry_call` insert sites pick up `'auto_hire'` without a signature change rippling through six callers. NULL means "pre-migration"; the backfill script (`scripts/backfill_observability.py`) promotes NULL to `'direct'` when no pipeline/compare/recipe/watcher join match is found.

**Retention.** `auto_hire_decisions` keeps 90 days of raw rows; older rows are aggregated to `auto_hire_decisions_daily` (migration 0051) keyed on `(day, reason, auto_invoked)`. The rollup table is kept indefinitely. The sweep runs at most once per 24h from inside `_jobs_sweeper_loop` (`server/application_parts/part_006.py::_maybe_run_decision_retention`); the work is in `core/observability.py::run_decision_retention`.

**MCP error code capture.** `mcp_invocation_log.error_code` (migration 0048) is populated when an MCP dispatch raises an `HTTPException` whose `detail` carries a structured `error.code`. Helper: `_extract_mcp_error_code` in `part_000.py`. Required for the `failures` view to be useful.

## Self-improving hosted skills (learnings memory)

Hosted SKILL.md skills accumulate "learnings" — short corrective bullets distilled from their own recent failures and injected at execution time. Hermes-style memory, not prompt rewriting: the stored `system_prompt` is never mutated, so reversal is a status flip. **Gated by `AZTEA_SELF_IMPROVEMENT` (default OFF, hosted-only).** When off, the whole feature is inert (no sweep, no injection, routes 404, ranking unchanged) — OSS stays byte-identical.

- **Table** `skill_learnings` (migration 0077): `learning_id`, `skill_id`, `agent_id`, `owner_id`, `text`, `status` (`proposed`|`active`|`archived`), `source_signal`, `source_job_ids`, `confidence`, timestamps. Status transitions ARE the version history (no hard deletes in the normal flow). `skill_id` has NO DB-level FK — `hosted_skills` already cascades from `agents`, so cleanup is app-side (the `DELETE /skills` route archives via `skill_learnings.archive_learnings_for_skill`). Migration 0077 also adds the `hosted_skills.last_distill_at` watermark.
- **Store** is `core/skill_learnings.py` (dedup vs proposed/active, pending cap, owner-scoped rowcount-guarded transitions, capped `active_learnings_block`).
- **Distiller** `core/skill_improvement.py::run_learning_distillation` runs once/24h off `_jobs_sweeper_loop` (`part_006.py::_maybe_run_learning_distillation`). Signals: low-rated jobs (authoritative rating from `job_quality_ratings`, joined to the already-scrubbed `output_examples` ring buffer by `job_id` — the example's own `rating` field is never populated at record time) + caller-filed dispute `reason`/`evidence` + judge `reasoning`. It does NOT read `caller_ratings.comment` — that table is the agent rating the *caller*, not buyer feedback, and `job_quality_ratings` has no comment column. Sensitive skills are skipped entirely via `core/privacy.is_example_sensitive_agent`, fed the AUTHORITATIVE flags (`pii_safe`/`outputs_not_stored` from the `agents` row, `category`/`examples_sensitive` from frontmatter) — NOT frontmatter alone. Caller free-text (dispute prose) and the distilled bullet are run through `core/privacy.scrub_freetext` (value-based secret/email scrub) since field-name redaction can't touch a token buried in prose. `run_with_fallback` soft-fails to a no-op.
- **Injection:** `core/skill_executor.py::_build_completion_request` reads the active block (flag-gated, soft-fail) and passes it into `build_messages`, which stays pure (the block is a parameter, wrapped as DATA inside the hardened prefix/suffix). Zero-learnings output is byte-identical to pre-0077 (regression-tested).
- **Owner routes** in `server/routes/skill_learnings.py` (`create_router`, wired in `part_011`): `GET /skills/{id}/learnings?status=` and `POST /skills/{id}/learnings/{lid}/decision`. Worker-scope + owner-match. Frontend panel: `frontend/src/features/builder/SkillLearningsPanel.jsx` on **My Agents**.
- **Privacy primitives** (`is_example_sensitive_agent`, `redact_sensitive`, the sensitive-ID set) were extracted from `part_003.py` to `core/privacy.py` so `core/` reuses them without importing `server/`. `part_003` re-imports them under their historical private names.

**Level-1 `trust_trend`.** `core/trust_trend.py` computes improving/flat/declining from `job_quality_ratings` (last-N vs prior-N avg; needs ~13+ ratings or it reads `unknown`). Attached to every enriched agent record by `reputation.enrich_agent_records` (display, always on). A small bounded ranking nudge (`trend_rank_delta` in `auto_hire._score_quality_signals`, `trend_blend_delta` in the `agents_ops` search blend) is **gated behind `AZTEA_SELF_IMPROVEMENT`** so the hot-path ranking change is opt-in.

Rollout + env knobs: `docs/runbooks/deploy.md` → "Self-improving hosted skills".

## Workspaces

- **Workspaces** are server-side shared state for one multi-agent workflow: a named collection of artifacts (named blobs) plus an optional Ed25519-signed seal manifest covering every artifact hash. Module owner is `core/workspaces.py`; tables are `workspaces` + `workspace_artifacts` (migrations 0053-0054). Buyer-facing docs at `docs/workspaces.md`. Operational runbook at `docs/runbooks/workspaces.md`.
- The new module re-exports `_local = _db._local` so `tests/integration/helpers._close_module_conn` works on it.
- **Reserved keys on `POST /registry/agents/{id}/call`:** `_workspace_id` (envelope, stripped before agent dispatch; triggers auto-write of the response under `outputs/{agent_slug}/{job_id}.json`) and `_artifact_ref: "ws_id/name"` (recursively substituted with artifact bytes/JSON/text/b64 per content-type before the agent sees the payload). Naming: underscore prefix marks them as dispatch-layer infrastructure so they never collide with an agent's own input schema fields.
- **Auth model:** owner can read/write/seal/delete. Workers with an active job lease on `workspace.run_id` can read/write but not seal or delete. `GET /workspaces/{id}/manifest`, `POST /workspaces/{id}/verify`, and `GET /workspaces/sealer/did.json` are PUBLIC. ID is 22-char base62 (~131 bits, unguessable).
- **Pipelines opt in** by setting `auto_workspace: true` on the recipe definition. `core/pipelines/executor.py:run_pipeline` creates a workspace per run, threads `_workspace_id` through every step's payload, writes step outputs under `outputs/{agent_slug}/{node_id}.json`, and seals on successful completion. `pipeline_runs.workspace_id` is surfaced by both run-status GET endpoints.
- **Signing key:** per-server Ed25519 at `data/workspace_signing_key.pem` (mode 0o600, .gitignored). Override path with `AZTEA_WORKSPACE_SIGNING_KEY_PATH`. DID: `did:web:<host>:workspaces:sealer`. Schema name: `aztea/workspace-seal/1`. Pattern mirrors the sandbox per-host signing key.
- **Body-size middleware** in `part_001.py` allows 8 MiB for `/workspaces/*/artifacts/*` paths (default cap stays 512 KB everywhere else). Per-artifact cap matches `core/sandbox/filesystem.py:_MAX_WRITE_BYTES`.
- **Sandbox backing:** `backing_type='sandbox'` + `backing_id` routes reads/writes through `core/sandbox/filesystem.py`. On `SandboxNotFound`, the workspace transitions to terminal `sandbox_evicted` state; the seal manifest can still be built from metadata rows.
- **What v0 doesn't have** (deferred to v0.1): auto-content-deletion for expired workspaces (sweeper marks status only), versioning, S3 backing, cross-user sharing, streaming I/O, per-workspace billing budget, frontend UI.
- **Test fixture change** in `tests/integration/conftest.py`: `isolated_db` now calls `apply_migrations(db_path)` so integration tests see the full schema — without this, `init_db()`'s `CREATE TABLE IF NOT EXISTS` path silently skipped columns added by later migrations.
