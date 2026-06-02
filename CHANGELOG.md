# Changelog

All notable changes to Aztea are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Aztea follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] - 2026-06-02

The agent-readable-web feature: `site_navigator` becomes a Firecrawl-class web
product (read the web, compile sites into APIs, prove provenance), plus the
write-web's safe foundation. Everything new ships behind default-OFF flags, so
this release is behavior-equivalent until an operator opts in.

### Added

- **HTTP-first + API discovery (Phase A, flag-gated).** `site_navigator` tries a
  plain SSRF-safe HTTP fetch and a signed API-spec replay before launching
  Chromium, and can return clean trafilatura markdown / HTML / links (goal
  optional). API specs bind an immutable signed scheme/host/port and are reused
  across authors only after a same-registrable-domain + signature check; replay
  routes through the existing DNS-rebind IP-pinning. Flags `AZTEA_HTTP_FIRST`,
  `AZTEA_API_DISCOVERY` (default off).
- **`/map`, `/crawl`, schema-validated `/extract` (Phase B).** Bounded BFS crawl
  with a per-call page cap and a total wall-clock budget.
- **Pluggable proxy/stealth fetch backend (Phase C).** Env-selected; off by default.
- **Public Firecrawl-shaped API + playground (Phase D).** `POST /scrape /map
  /crawl /extract` and a public, offline `POST /web/verify`; a React playground
  at `/web`. Gated by `AZTEA_WEB_API_ENABLED` (default off).
- **Signed proof-of-observation receipts.** Provenance, not truth; verifiable
  offline. The verdict's `signer_did` is checked against the agent's real
  did:web, so a caller cannot sign with their own key yet claim another identity.
- **Write-web foundation (Phase E).** `web_actor` interact-then-reveal (E1: safe,
  no money, bounded), typed action mandates with allowed-domain enforcement,
  escrow split math, and the fail-FORWARD `web_actions` state machine. The
  commit/escrow paths are default-OFF scaffold; the live ledger movement is a
  separate, `/cso`-gated money-PR.
- **Shared-map royalty obligation recording (Phase F).** Records the payable
  idempotently; moves no money (the funded credit is the money-PR).

### Fixed

- SSRF "blocks-private-by-default" tests now enforce the guard regardless of the
  dev `.env` `ALLOW_PRIVATE_OUTBOUND_URLS=1` (the enforcement fixture was scoped
  only to `tests/security/`).
- Registration verifier test mock now echoes the required `payload_hash` binding
  (2026-05-22 anti-replay hardening).
- Curated catalog count 10 -> 11 (`site_navigator` added) across the snapshot
  test and docs.

## [1.3.0] - 2026-05-29

Marketplace hardening release on top of v1.2.1's auto-hire ranker overhaul.
Two parallel feature branches (Wave 2 + Wave 3) landed first; then a
four-phase architectural pass made the platform credibly framework-agnostic —
any developer, any agent stack, can list on Aztea and the platform stands
behind it on both buyer and seller sides.

PR-93 migrations renumbered to 0072–0076 to sit on top of v1.2.1's
0067–0071 series.

### Added

- **HMAC signing of every outbound agent call.** Per-agent shared secret
  surfaced once at registration; sellers verify on inbound. Stops freeloaders
  from calling endpoints directly without paying.
- **`aztea.verify` SDK helper** for seller-side signature verification.
  Drop-in for FastAPI / Hono / any Python HTTP framework.
- **`aztea wrapper init` CLI subcommand** scaffolds a deploy-ready FastAPI
  server + Dockerfile + `fly.toml`/`render.yaml` for sellers wrapping
  LangGraph, CrewAI, MCP, or custom-Python agents. 40-line wrapper template.
- **`POST /registry/agents/{id}/rotate-secret`** owner-scoped rotation route
  with one-time-display reveal in the registration response and on MyAgentsPage.
- **`POST /registry/agents/{id}/verify-domain`** route. Sellers prove they
  own the endpoint domain via `.well-known/aztea-agent.json` or DNS TXT
  (`_aztea-agent.<host>`). Earns a "Domain verified" badge on the agent
  detail page and a +5 auto-hire ranking bonus.
- **Continuous endpoint health sweeper** (`core/observability.run_endpoint_health_sweep`).
  Hourly probe with HMAC signature; auto-suspends agents after 3 consecutive
  failures with `suspension_reason=health_check_failed`.
- **Output-example replay at probe time.** Sellers' declared `output_examples`
  are now POSTed to their endpoint at registration; mismatched shape emits a
  WARN finding. Sellers using the wrapper template correctly return 401 and
  are not noise-flagged.
- **jsonschema validation guardrail in `auto_call_agent`.** LLM-extracted
  payloads are now validated against the agent's `input_schema` BEFORE
  invocation. Returns `reason='schema_validation_failed'` with actionable
  error messages instead of silently refunding the buyer.
- **Claude pin for the auto-hire LLM extractor.** Single-field extraction uses
  Claude Haiku 4.5 (~$0.0015/decision); multi-field uses Sonnet 4.6
  (~$0.005/decision). `json_mode=True` stops fence-wrapping. Env-overridable
  via `AZTEA_AUTO_HIRE_EXTRACTION_MODEL_SINGLE`/`_MULTI`.
- **Wave 2 features:**
  - MCP rename `search_specialists` → `search_agents` (+ 3 more) with
    bidirectional aliases so old MCP clients keep working.
  - `/publish_agent` MCP tool — consumer-to-supplier conversion path.
  - `aztea publish` CLI gets an AI-inferred third option (AST-based metadata).
  - Two new PyPI packages: `aztea-anthropic` (Anthropic Messages + Agents
    SDK adapter), `aztea-langchain` (LangChain tool adapter).
  - Python SDK `client.agents.*` namespace (mirrors TS SDK shape).
  - Public builder profile pages at `/builders/<username>` plus
    `?owner_id=...` filter on `/agents`.
  - Four new reference docs: `auth-billing.md`, `rate-limits.md`,
    `idempotency.md`, `webhooks.md`.
- **Wave 3 features:**
  - Browser playground at `/build` with template gallery, "Test in sandbox"
    button, and one-click publish path.
  - `POST /api/playground/test` + `POST /api/playground/publish` routes,
    gated by `AZTEA_PLAYGROUND_ENABLED`.
  - LLM-based listing-safety judge (`core/listing_safety_judge.py`) layered
    on top of the static scanner. Env-toggleable via `AZTEA_LISTING_JUDGE`.
  - Admin kill switch (`POST /admin/agents/{id}/suspend`) with atomic
    in-flight job refund.
  - `hosted_execution_log` audit table (migration 0072) with 30-day retention
    for anonymous playground rows.
  - Sandbox escape test suite (`tests/security/test_sandbox_escape.py`).
- **Wave 1 features:**
  - Catalog cull 27 → 10 curated agents (only those demonstrating a
    platform primitive stay).
  - Public anonymous `/api/integrations/openai-tools.json` and
    `/api/integrations/gemini-tools.json` endpoints.
  - Error envelope refactor across ~80 routes for consistent
    `{error: {code, message}}` shape.
  - SEO meta + per-route page titles.
  - Stripe Connect 5-state derivation on `/wallet`.
  - CSV export button on `/my-agents`.

### Changed

- Pre-existing `/skills` publish path **reopened** to non-master callers.
  Non-master publishes land in `review_status='probation'` (price-capped,
  rank-penalized) until track record graduates them. Static scanner + LLM
  judge enforce safety on every public submission.
- Outbound agent calls now serialize the body once (canonical JSON with
  sorted keys) and send via `data=` rather than `json=` so the HMAC
  signature covers the exact bytes the seller receives.
- `_proxy_headers_for_agent` (`part_002.py`) accepts optional `body`,
  `job_id`, `caller_owner_id` kwargs and emits `X-Aztea-Signature` +
  `X-Aztea-Timestamp` headers when a signing secret is present.
- Built-in recipe list culled to `audit-deps` + `domain-health` only.

### Fixed

- **HIGH severity SSRF** in `core/domain_proof.verify_well_known`:
  re-validate constructed `.well-known` URL through
  `core.url_security.validate_outbound_url` before the GET. DNS rebinding
  between registration and verification can no longer land probes on
  private IPs.
- **HIGH severity SSRF** in `core/observability._probe_endpoint_health`:
  same SSRF re-check before every hourly health probe. Rebinding endpoints
  now fail-closed and progress toward suspension.
- **NaN/Inf timestamp bypass** in `core/crypto._parse_iso_or_epoch` and
  `sdks/python-sdk/aztea/verify`: `float('nan')` previously slipped past
  `abs(now - ts) > window` silently. Now rejected with `InvalidSignature`.
- **Wrapper template billing surface**: generated `server.py` now
  short-circuits Aztea's hourly `{"_aztea_health": true}` probe before
  invoking the seller's `handler()`. Sellers no longer pay LLM/compute on
  every health check.
- **Health-sweep concurrent-pass race**: optimistic-concurrency guard
  (`UPDATE ... WHERE consecutive_health_failures = <expected>`) on the
  streak counter. Racing sweepers detect via `rowcount=0` and skip.
- **Output-example replay false-positives** on hardened sellers: a 401/403
  response is now treated as "endpoint correctly refused unsigned probe"
  instead of a contract failure.
- `frontend/src/pages/WorkspacesPage.jsx`: Button icon prop was passed
  as a bare component class (`icon={Trash2}`) instead of an element
  (`icon={<Trash2 size={14} />}`). Caused a runtime React error when
  Workspaces loaded; aligned with codebase convention.
- Stale assertions in 3 tests post-Wave-3 (SKILL.md publish path,
  admin IP allowlist response shape).
- GitHub Actions in new PyPI publish workflows pinned to commit SHAs
  rather than floating major tags. Supply-chain hardening on the path
  that holds PyPI tokens.

### Test infrastructure

- `fake_post()` mocks across 7 integration test files updated to accept
  the new `data=body_bytes` dispatch kwarg used by HMAC-signed calls.
- `test_envelope_contract.py` fixture now saves and restores
  `module.DB_PATH` originals on teardown — direct assignment isn't tracked
  by `monkeypatch`, so the leak was polluting every later test that
  touched the registry via the same module global.
- `HealthResponse` schema gains optional `db: str` and
  `llm_providers: list[str]` fields so SDK consumers typing the
  lightweight `/health` route's flat response against `HealthResponse`
  compile cleanly.

### Migrations (this release)

- `0072_hosted_execution_log.sql` — anonymous-playground audit table
- `0073_users_profile_visible_earnings.sql` — opt-in earnings on builder profile
- `0074_agent_endpoint_signing_secret.sql` — per-agent HMAC secret + rotated_at + index
- `0075_agent_consecutive_health_failures.sql` — health-sweep streak counter
- `0076_agent_domain_verification.sql` — domain-verified badge + verified_at + method

## [1.2.1] - 2026-05-29

Auto-hire ranker overhaul. 12 ranker improvements + reflex/eval foundation,
all behind feature flags (defaults preserve current behavior). Multi-layered
defense against LLM cost amplification, Sybil ranking sabotage, and prompt
injection. Merged on top of v1.2.0 (#91 buyer-agent call-path speedup);
`auto_hire.decide_cached` now threads `caller_owner_id` through the cache
key so per-caller affinity bias remains correct under caching, and
`decision_audit._write_row` carries this PR's three forward-only columns
(`feature_vector_json`, `shadow_chosen_agent_id`, `intent_class`) on
PR #91's deferred-queue persistence path.

### Added

- **Phase 0.5 reflex track** — MCP tool descriptions for `do_specialist_task`,
  `search_specialists`, `manage_workflow` rewritten to lead with WHEN, not WHAT,
  covering the full specialist catalog (CVE, DNS, sandboxed execution, audit,
  lint, infra validation, web automation, document parse, protocol debug, load
  test). CLAUDE.md routing copy rewritten with identity framing and explicit
  trigger list.
- **B3 lemmatized keyword matching** (`core/registry/auto_hire.py`) — curated
  `match_keywords` / `block_keywords` now match across plural/conjugated forms
  ("audit" hits "auditing", "cve" hits "cves") via `simplemma` with a pure-Python
  suffix-strip fallback. Behind `auto_invoke_lemmatize_keywords` (default on).
- **C2 stability auto-flip** (`core/registry/stability_monitor.py`,
  `migrations/0067_stability_auto_flip.sql`) — sweeper-driven override of
  `agents.stability_override` that auto-marks an agent `broken` when its
  endpoint-error rate crosses threshold, and auto-clears on a clean recovery
  streak. Per-flip audit log in new `stability_flip_history` table.
- **C1 schema-driven payload extraction** — one LLM call fills the full payload
  dict against the agent's JSON Schema, replacing N per-field calls.
- **C3 per-caller affinity scoring** (`core/registry/caller_affinity.py`) —
  small ±8 bonus toward agents the caller has previously 5-starred, sourced via
  a new `core/reputation.py::caller_agent_affinity` boundary helper.
- **C4 utility-aware scoring** — agent latency from `avg_latency_ms` penalises
  slow agents in ranking (bounded ±6).
- **B4 LLM tiebreaker** (`core/registry/llm_tiebreaker.py`) — when confidence
  sits in [floor − 0.15, floor), one LLM call picks the best of the top 3
  candidates. Hallucination-safe: only returns agents from the input list.
- **C5 compound intent detection** (`core/registry/compound_intent.py`) —
  multi-step intents ("audit my repo and post findings to Slack") refuse with
  `compound_intent` reason + recipe pointer instead of force-fitting one half
  into the top candidate's payload.
- **Phase 2 intent taxonomy** (`core/registry/intent_taxonomy.py`,
  `core/registry/intent_classifier.py`) — 7-label COARSE taxonomy
  (`code_execution`, `code_audit`, `infra_check`, `live_data`, `document_parse`,
  `web_automation`, `other`) with a hybrid rule + LLM classifier. Background
  thread populates the cache so the hot path is never blocked.
- **B1 per-agent example intents** (`core/registry/example_intents.py`,
  `migrations/0070_agent_example_intents.sql`) — generate 10-20 canonical user
  intents per agent at registration, with sanitization defending against prompt
  injection via agent description.
- **Phase 3 per-class success tracking** (`migrations/0069_intent_class_success_rollup.sql`)
  — Beta-distribution posterior of success rate per (agent, intent_class),
  replacing the ad-hoc anti-catchall + cold-start penalties with a principled
  bounded ±8 bonus. Driven by a new rollup table the observability sweeper
  populates.
- **Phase 3.5 feature logging** (`migrations/0068_auto_hire_feature_logging.sql`)
  — write-only `feature_vector_json` + `shadow_chosen_agent_id` + `intent_class`
  columns on `auto_hire_decisions`, capturing training data for the eventual
  learned ranker without touching the read path.
- **Phase 4 learned-ranker scaffold** (`core/registry/learned_ranker.py`,
  `migrations/0071_ranker_model_weights.sql`) — storage + load + inference for
  logistic-regression weights with Platt-scaled calibration. Cross-backend
  upsert (`ON CONFLICT`). Honest framing: ships as imitation + calibration, not
  policy improvement, until training data accumulates.
- **Reflex eval harness scaffold** (`tests/eval/reflex/`) — JSON-schema-validated
  fixtures + runner skeleton ready for headless Claude Code SDK integration.

### Changed

- **`do_specialist_task` is now single-call by default** — `dry_run=true` is
  still accepted (backward-compat) but the response includes a deprecation hint
  steering callers to the single-call shape. The router refuses for free when
  no agent matches, so the two-step preview is rarely worth the round-trip.
- **Refusal taxonomy locked** (`core/error_codes.py::AUTO_HIRE_REASONS`) —
  every refusal reason emitted by `decide()` now has a stable code with the
  `auto_hire.` prefix. Additive-only stability promised.
- **`core/output_formats.py::render_refusal`** — auto-hire refusal envelopes
  now render through the same `output_format` (`markdown`, `github_pr_comment`,
  `slack_blocks`, `text`) pipeline that successful outputs use.
- **`decision_audit.record_decision`** squashed to a single atomic INSERT
  (was two-phase INSERT + UPDATE which had a transactional asymmetry on the
  failure path), with backward-compat fallback for pre-0068 environments.
- **Feature flag dependency enforcement** — `core/feature_flags.py` adds 9
  Phase 1-5 flags plus `check_auto_invoke_flag_dependencies()` that warns on
  unmet prerequisites (e.g. learned ranker requires calibrated confidence).

### Security

- **Per-caller LLM budget** (`core/registry/_llm_budget.py`) — three independent
  layers: per-request `RequestBudget` (caps amplification per orchestration),
  per-caller bucket (one owner can't drain global), global bucket (system
  ceiling). On layer-1 failure, downstream layers refund correctly.
- **Sybil-defense gating on catchall and stability flips** — catchall demotion
  requires ≥3 distinct caller owners AND ≥14-day agent age AND ≥20 total
  decisions before applying. Stability auto-flip requires errors from ≥3
  distinct caller owners; recovery requires clean signals from ≥3 distinct
  callers AND a 6-hour minimum hold from the last broken flip.
- **Prompt injection defense in depth** — `example_intents._sanitize_for_prompt`
  strips known LLM injection markers (`</system>`, `[INST]`, `<|im_start|>`,
  "ignore previous", unicode tag blocks) on both prompt input AND LLM output,
  with `<AGENT_DATA>` delimiter wrapping to mark untrusted regions.
- **Whole-payload extractor type+bounds enforcement** — LLM-extracted JSON now
  enforces declared JSON Schema types (string-only default when unspecified)
  plus depth ≤5, string ≤8KB, list ≤256, dict keys ≤64. Defeats
  prompt-injected nested-payload smuggling.
- **Stability monitor TOCTOU fix** — `_apply_flip` switched from
  SELECT-then-UPDATE (race window allowing operator-suspend bypass) to a single
  conditional `UPDATE … WHERE status NOT IN ('banned', 'suspended')` with
  rowcount check.
- **`caller_affinity` bounded LRU** (`OrderedDict` + `threading.Lock`) — caps
  cache at 8192 entries with last-touched eviction; DB read happens outside the
  lock to avoid serializing all callers.
- **Cost-burst observability** — LLM budget exhaustion logs structured warnings
  throttled to 1/min per category so attacks don't amplify the log volume.

### Fixed

- `core/registry/auto_hire.py::_llm_extract_field` and the new whole-payload
  extractor now pass `model=""` to `CompletionRequest` (the field is required;
  the prior callers silently swallowed the `TypeError` via the broad `except`).
- Endpoint-error classification in `stability_monitor` no longer counts NULL
  `error_message` failures as endpoint-side — pydantic ValidationErrors that
  serialize with null `error_message` are now treated as ambiguous, not
  agent-side.
- `learned_ranker.register_model` switched from SQLite-only `INSERT OR REPLACE`
  to cross-backend `INSERT ... ON CONFLICT` (Postgres prod would have crashed).

### Operations

- New env var prefix `AZTEA_AUTO_INVOKE_*` for the 9 Phase 1-5 rollout flags.
- New env var prefix `AZTEA_STABILITY_*` for the auto-flip tunables.
- New env var prefix `AZTEA_LLM_BUDGET_*` for the per-category token bucket
  capacities and per-caller fractions.

## [1.1.1] - 2026-05-28

Security patch: address all 8 /cso findings (HIGH lockfile + 6 MED + 1 LOW). See `#89`.
