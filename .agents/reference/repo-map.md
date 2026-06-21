# Repository map

> Resolved reference for `CLAUDE.md`. Read when you need to locate a module or understand how the tree is organised.

Every Python source file is **< 1000 lines** (see `engineering-style.md`). Large modules are split into cohesive packages whose `__init__.py` re-exports the merged public surface so `import core.jobs as jobs` continues to behave like a single module. `scripts/check_file_line_budget.py` enforces this.

```
server/
  application.py                 Thin entrypoint; loads ordered shards into one namespace
  application_parts/             Ordered implementation shards (part_000.py … part_013.py)
  application_parts/part_000.py  Imports, env/config, logging, Sentry, agent IDs + constants
  application_parts/part_001.py  Migrations, FastAPI app + lifespan, CORS, /api/* compat shim,
                                 security headers, request tracing, Prometheus metrics
  application_parts/part_006.py  Background sweeper, onboarding routes, auth routes
  application_parts/part_012.py  Hosted skills API (SKILL.md upload/run/list)
  application_parts/part_013.py  SPA fallback: serves frontend/dist/index.html for non-API paths
  builtin_agents/                Built-in IDs (constants.py), schemas (schemas.py), specs
  builtin_agents/constants.py    All AGENT_ID constants, BUILTIN_INTERNAL_ENDPOINTS,
                                 CURATED_PUBLIC_BUILTIN_AGENT_IDS, SUNSET_DEPRECATED_AGENT_IDS
  builtin_agents/specs.py        Merges specs_part1 + specs_part2; returns curated public builtins
  error_handlers.py              Shared HTTPException / validation / rate-limit handlers
  routes/system.py               Small sub-router for system routes

agents/                          Built-in agent implementations (one module each)
  cve_lookup.py                  NIST NVD live API
  python_executor.py             Subprocess sandbox (real Python execution)
  multi_language_executor.py     Polyglot code execution (Node/Deno/Bun/Go/Rust)
  db_sandbox.py                  SQLite sandbox (isolated tempfile DB)
  live_sandbox.py                Persistent sandbox + DB (9 verbs)
  browser_agent.py               Playwright-based headless browsing
  dependency_auditor.py          Package CVE + license audit via live NVD data
  dns_inspector.py               DNS record, SSL cert, HTTP metadata live lookup
  secret_scanner.py              Repo / file secret detection
  sast_scanner.py                Static security analysis
  lighthouse_auditor.py          Lighthouse audit via Playwright
  accessibility_auditor.py       axe-core accessibility audit
  broken_link_crawler.py         Live HTTP crawl + status check
  pdf_document_parser.py         PDF extraction + structured output
  jwt_validator.py               JWT decode + signature verify (HS/RS/ES via PyJWK)
  stripe_webhook_debugger.py     Stripe webhook signature + payload debug
  load_tester.py                 Bounded HTTP load test
  ci_failure_reproducer.py       CI log fetch + minimal reproduction
  dockerfile_analyzer.py         Dockerfile lint + best-practices grade
  openapi_validator.py           OpenAPI spec validation
  coverage_runner.py             Python coverage run in sandbox
  k8s_manifest_validator.py      Kubernetes YAML schema + policy lint
  terraform_plan_analyzer.py     Terraform plan parse + risk surface
  hcl_terraform_analyzer.py      Static HCL lint via checkov rules
  # Sunsetted (file remains, but excluded from CURATED_PUBLIC — endpoints stay
  # wired so old job IDs / signed receipts continue to resolve. See
  # SUNSET_DEPRECATED_AGENT_IDS in server/builtin_agents/constants.py):
  #   docs_grounder.py, diff_analyzer.py, unicode_inspector.py,
  #   regex_tester.py, ssl_certificate_decoder.py, pypi_metadata.py,
  #   github_releases.py, security_headers_grader.py, sbom_generator.py,
  #   web_search.py, visual_regression.py, archive_inspector.py

core/
  db.py                          Dual-backend connection manager (Postgres + SQLite); thread-local pool;
                                 normalises %s placeholders to ? for SQLite. Owns IntegrityError /
                                 OperationalError / ProgrammingError exports so callers never import
                                 from sqlite3 or psycopg2 directly. Backend chosen by DATABASE_URL.
  migrate.py                     Idempotent migration runner (apply_migrations)
  auth/                          Users + scoped keys (schema.py, users.py)
  registry/                      Agent listings + auto-hire decision logic + embeddings cache
  jobs/                          Async job lifecycle: db.py, crud.py, leases.py, messaging.py
  payments/                      Wallets + insert-only ledger (base.py) + dispute helpers (trust_disputes.py)
  models/                        Pydantic v2 contracts: core_types, job_requests, messages_ops, responses
  mcp_manifest.py                registry → MCP tool manifest (snake_case keys, no prefix)
  embeddings.py                  sentence-transformers backend
  disputes.py                    Dispute lifecycle and bilateral caller ratings
  judges.py                      LLM-based dispute + quality judge logic
  reputation.py                  Trust scores — SOLE owner of the caller_ratings table
  onboarding.py                  agent.md parsing/validation/ingestion
  error_codes.py                 Machine-readable error taxonomy
  url_security.py                SSRF validation for all outbound URLs
  payout_curve.py                Quality-adjusted payout clawbacks (compensating entries)
  compare.py                     Compare-job orchestration (same task across N agents)
  pipelines/                     Multi-step pipeline execution and persistence
  recipes.py                     Saved pipeline templates
  tool_adapters.py               Shared MCP-manifest builders (OpenAI / Gemini / A2A adapters)
  feature_flags.py               Runtime feature toggles (env-based, no caching)
  skill_executor.py              Hosted SKILL.md execution engine
  skill_parser.py                SKILL.md parser / validator
  hosted_skills.py               DB layer for uploaded skills
  identity.py                    Agent DID / Ed25519 key generation and signing
  crypto.py                      Signing primitives used by identity.py
  cache.py                       Result cache for deduplication (TTL-based)
  output_shaping.py              Response normalisation / truncation
  observability.py               Prometheus metrics helpers (incl. `job_duration_seconds`
                                 histogram, `builtin_agent_calls_total` counter), Sentry
                                 breadcrumb helpers. `GET /health` lives in `part_001.py`
                                 and returns {status, db, llm_providers, version}.
  fastpath.py                    Short-circuit for cache-hit and zero-price calls
  email.py                       SMTP email dispatch (no-ops silently if SMTP_HOST unset)
  llm/
    base.py                      Message, CompletionRequest, LLMResponse, LLMProvider Protocol, Usage
    errors.py                    LLMError hierarchy: rate limit, timeout, auth, bad response
    registry.py                  PROVIDERS dict, resolve(spec), DEFAULT_CHAIN
    fallback.py                  run_with_fallback() — chain-tries providers, retries on rate limit
    providers/                   groq, openai, anthropic, cohere, bedrock, openai_compatible (25+ via env)
  job_events.py                  Fire-and-forget HTTP POST to Elixir sidecar on job state transitions.
                                 Never raises, never blocks. Gated by AZTEA_ELIXIR_EVENTS=1.

elixir/                          Phoenix/OTP sidecar — Step 1 of strangle-fig migration
  lib/aztea_web/                 Endpoint + Router + EventController + UserSocket + JobChannel
  lib/aztea_web/token.ex         HMAC-SHA256 short-lived socket auth (shared secret with Python)
  lib/aztea/jobs/                JobServer / Sweeper GenServers (legacy, pre-migration)
  config/                        runtime.exs reads ELIXIR_HTTP_PORT, ELIXIR_INTERNAL_SHARED_SECRET
  Service: aztea-elixir.service  Phoenix listens on 127.0.0.1:4000; Caddy proxies /elixir/socket

migrations/
  0001_initial.sql               Canonical schema — all CREATE TABLE / INDEX
  0002–0077_*.sql                Incremental additions (applied once via schema_migrations table)

sdks/
  python-sdk/                    AzteaClient (hire), AgentServer (@handler + polling loop)
  typescript/                    TypeScript SDK

frontend/
  src/api.js                     All API calls go through here; normalises errors, handles 401 lifecycle
  src/context/MarketContext.jsx  Global state: agents, wallet, jobs, runs; 20s polling refresh
  src/context/AuthContext.jsx    Session state and API-key management
  src/features/auth/AuthPanel.jsx  Login / register
  src/features/agents/           AgentCard, AgentInputForm, TrustGauge
  src/features/agents/results/   ResultRenderer + per-agent result components
  src/features/jobs/JobTimeline  Job status timeline component
  src/pages/                     One file per route
  src/ui/                        Design-system primitives: Button, Card, Badge, Input, Pill, Select
  src/ui/motion/                 Animation primitives: Reveal, Stagger, NumberMorph, Counter
  src/utils/inputGuards.js       Client-side validators
  src/utils/format.js            fmtDate, fmtUsd, fmtMs, relativeTime — import here, never redefine
  src/theme/tokens.css           CSS custom properties for all colours, spacing, radii, typography

scripts/
  aztea_mcp_server.py            Compat shim (~30 lines) — delegates to aztea.mcp.server (the
                                 real stdio MCP server lives in sdks/python-sdk/aztea/mcp/)
  check_file_line_budget.py      CI enforcement for the 1000-line rule

tests/
  integration/                   Split integration suite — helpers in support.py and helpers.py
  test_bug_regressions.py        Regression tests for previously fixed bugs
  test_agent_real_tool.py        Agent contract tests
  test_mcp_manifest.py           MCP manifest correctness and schema-mutation safety

docs/
  runbooks/                      Operational runbooks (deploy, ledger drift, runtime prereqs, smoke test)
  api-reference.md               Full HTTP API reference
  quickstart.md                  MCP / Claude Code quickstart
  agent-builder.md               Guide for registering and running agents
  orchestrator-guide.md          Multi-agent pipeline guide
  mcp-integration.md             MCP server setup and tool catalogue
  skill-md-reference.md          SKILL.md format reference
  stripe-setup.md                Stripe Connect and webhook configuration
  errors.md                      Error code taxonomy
  reputation.md                  Trust score formula and rating mechanics

docker-compose.yml               Dev compose (no SSL, mounts ./data)
Makefile                         Dev shortcuts: make dev / test / docker / migrate
```
