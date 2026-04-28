# Aztea A/A+ Recommendations

Date: 2026-04-27

## Bottom line

I do **not** fully agree with Claude's Phase 2 prompt as written.

## Verification Update: Phase 0 / Phase 1

## Implementation Update: Phase 5 performance work

I completed the practical Phase 5 performance slice that materially affects coding-agent UX.

### Landed

- new [core/output_shaping.py](/Users/aakritigarodia/Desktop/agentmarket/core/output_shaping.py)
  - summary/full output shaping
  - truncation for long strings, large lists, large dicts
  - base64/blob replacement with compact artifact metadata
- cache v2 normalization in [core/cache.py](/Users/aakritigarodia/Desktop/agentmarket/core/cache.py)
  - version-aware cache keys
  - `original_job_id` preserved on hits
  - cacheability gate for internal executors
- sync call shaping and cache wiring in [server/application_parts/part_008.py](/Users/aakritigarodia/Desktop/agentmarket/server/application_parts/part_008.py)
- shaped job responses and `GET /jobs/{id}/full`
  - [server/application_parts/part_002.py](/Users/aakritigarodia/Desktop/agentmarket/server/application_parts/part_002.py)
  - [server/application_parts/part_009.py](/Users/aakritigarodia/Desktop/agentmarket/server/application_parts/part_009.py)
- lazy MCP schema mode in [scripts/aztea_mcp_server.py](/Users/aakritigarodia/Desktop/agentmarket/scripts/aztea_mcp_server.py)
  - `aztea_search`
  - `aztea_describe`
  - `aztea_call`
- Python warm pool in [agents/python_executor.py](/Users/aakritigarodia/Desktop/agentmarket/agents/python_executor.py)
- explicit local fast-path helper in [core/fastpath.py](/Users/aakritigarodia/Desktop/agentmarket/core/fastpath.py)
  - wired into [core/pipelines/executor.py](/Users/aakritigarodia/Desktop/agentmarket/core/pipelines/executor.py)

### Verification status

- `python -m py_compile` on touched Phase 5 files: **passed**
- direct harnesses for:
  - output shaping
  - lazy MCP mode
  - cache v2 identity behavior
  - Python warm pool
  - **passed**
- `server` import after the Phase 5 changes: **passed**

### Judgment

This is enough to count Phase 5 as materially done for the current product direction. The main remaining work is not backend capability anymore; it is buyer-surface polish and rollout confidence.

## Implementation Update: Phase 6 DX overhaul

I then completed the core Phase 6 developer-experience cutover.

### Canonical SDK

- `sdks/python-sdk/` is now the canonical Python SDK
- new shared auth/config store:
  - [sdks/python-sdk/aztea/config.py](/Users/aakritigarodia/Desktop/agentmarket/sdks/python-sdk/aztea/config.py)
- richer result rendering:
  - [sdks/python-sdk/aztea/results.py](/Users/aakritigarodia/Desktop/agentmarket/sdks/python-sdk/aztea/results.py)
  - SDK models now implement `__rich__`
  - job-bearing models now expose `.full()`
- improved SDK error formatting in [sdks/python-sdk/aztea/errors.py](/Users/aakritigarodia/Desktop/agentmarket/sdks/python-sdk/aztea/errors.py)
  - code/message/hint string formatting
- new convenience client methods in [sdks/python-sdk/aztea/client.py](/Users/aakritigarodia/Desktop/agentmarket/sdks/python-sdk/aztea/client.py)
  - `/jobs/{id}/full`
  - top-up session creation
  - pipeline/recipe helpers

### Legacy shim

- `sdks/python/` is now a deprecation shim over the canonical package
- new shim loader:
  - [sdks/python/aztea/_shim.py](/Users/aakritigarodia/Desktop/agentmarket/sdks/python/aztea/_shim.py)
- old imports still resolve, but emit `DeprecationWarning`

### Real CLI

- new [sdks/python-sdk/aztea/cli.py](/Users/aakritigarodia/Desktop/agentmarket/sdks/python-sdk/aztea/cli.py)
- console script declared in [sdks/python-sdk/pyproject.toml](/Users/aakritigarodia/Desktop/agentmarket/sdks/python-sdk/pyproject.toml)
- wrapper [scripts/client_cli.py](/Users/aakritigarodia/Desktop/agentmarket/scripts/client_cli.py) now points to the real CLI
- landed commands:
  - `aztea login`
  - `aztea logout`
  - `aztea agents list`
  - `aztea agents show`
  - `aztea hire`
  - `aztea call`
  - `aztea jobs status`
  - `aztea jobs follow`
  - `aztea wallet balance`
  - `aztea wallet topup`
  - `aztea pipelines run`
- all of the above support `--json`

### TUI polish

- TUI now imports the canonical SDK path:
  - [tui/aztea_tui/api.py](/Users/aakritigarodia/Desktop/agentmarket/tui/aztea_tui/api.py)
- TUI config now reuses the shared SDK config store:
  - [tui/aztea_tui/config.py](/Users/aakritigarodia/Desktop/agentmarket/tui/aztea_tui/config.py)
- added recent jobs pane with live stream tail:
  - [tui/aztea_tui/widgets/recent_jobs.py](/Users/aakritigarodia/Desktop/agentmarket/tui/aztea_tui/widgets/recent_jobs.py)
  - wired into [tui/aztea_tui/screens/main.py](/Users/aakritigarodia/Desktop/agentmarket/tui/aztea_tui/screens/main.py)
  - styled in [tui/aztea_tui/aztea.tcss](/Users/aakritigarodia/Desktop/agentmarket/tui/aztea_tui/aztea.tcss)

### Docs

- canonical SDK readme rewritten:
  - [sdks/python-sdk/README.md](/Users/aakritigarodia/Desktop/agentmarket/sdks/python-sdk/README.md)
- legacy readme trimmed to shim guidance:
  - [sdks/python/README.md](/Users/aakritigarodia/Desktop/agentmarket/sdks/python/README.md)
- TUI docs updated for:
  - shared login state
  - canonical SDK path
  - lazy MCP tool shape
  - recent jobs pane
  - [tui/README.md](/Users/aakritigarodia/Desktop/agentmarket/tui/README.md)

### Verification status

- `python -m py_compile` on all touched Phase 6 SDK/TUI/CLI files: **passed**
- direct CLI harness:
  - `login --api-key --json`
  - `hire --input @file.json --json`
  - `agents list --search --json`
  - **passed**
- TUI import harness after SDK path/config changes: **passed**
- legacy shim import harness:
  - imports resolve through `sdks/python`
  - deprecation warning emitted
  - **passed**

### Judgment

This finishes the main “kill the ugly curl” objective. Aztea now has one canonical Python SDK, one shared token store, a real first-party CLI, and a TUI that no longer lives on a parallel auth path.

## Follow-up completion: remaining Phase 5 / 6 cleanup

I then closed the remaining loose ends I found while wiring the first Phase 7 agents.

### Landed

- `AZTEA_LAZY_MCP_SCHEMAS` now defaults on in [core/feature_flags.py](/Users/aakritigarodia/Desktop/agentmarket/core/feature_flags.py)
- `cacheable` is now a real persisted agent attribute instead of a pure heuristic:
  - [core/registry/core_schema.py](/Users/aakritigarodia/Desktop/agentmarket/core/registry/core_schema.py)
  - [core/registry/agents_ops.py](/Users/aakritigarodia/Desktop/agentmarket/core/registry/agents_ops.py)
  - [core/models/core_types.py](/Users/aakritigarodia/Desktop/agentmarket/core/models/core_types.py)
  - [server/application_parts/part_007.py](/Users/aakritigarodia/Desktop/agentmarket/server/application_parts/part_007.py)
  - [server/application_parts/part_001.py](/Users/aakritigarodia/Desktop/agentmarket/server/application_parts/part_001.py)
- the stale canonical SDK `setup.py` was aligned with the real package shape:
  - [sdks/python-sdk/setup.py](/Users/aakritigarodia/Desktop/agentmarket/sdks/python-sdk/setup.py)

### Judgment

At this point the remaining Phase 5 / 6 work is not backend capability. It is rollout, packaging, and broader regression execution.

## Implementation Update: Phase 7 first tranche

I implemented the first three high-signal Phase 7 agents: the ones that add concrete external capability without depending on heavier browser/runtime infrastructure.

### Landed

- new agents:
  - [agents/db_sandbox.py](/Users/aakritigarodia/Desktop/agentmarket/agents/db_sandbox.py)
  - [agents/visual_regression.py](/Users/aakritigarodia/Desktop/agentmarket/agents/visual_regression.py)
  - [agents/live_endpoint_tester.py](/Users/aakritigarodia/Desktop/agentmarket/agents/live_endpoint_tester.py)
- builtin registration / IDs / dispatch:
  - [server/builtin_agents/constants.py](/Users/aakritigarodia/Desktop/agentmarket/server/builtin_agents/constants.py)
  - [server/builtin_agents/specs_part4.py](/Users/aakritigarodia/Desktop/agentmarket/server/builtin_agents/specs_part4.py)
  - [server/builtin_agents/specs.py](/Users/aakritigarodia/Desktop/agentmarket/server/builtin_agents/specs.py)
  - [server/application_parts/part_000.py](/Users/aakritigarodia/Desktop/agentmarket/server/application_parts/part_000.py)
  - [server/application_parts/part_004.py](/Users/aakritigarodia/Desktop/agentmarket/server/application_parts/part_004.py)
- runtime dependency for image diffing:
  - [requirements.txt](/Users/aakritigarodia/Desktop/agentmarket/requirements.txt)

### Agent quality notes

- **DB Sandbox**
  - ephemeral SQLite only
  - progress-handler timeout guard
  - DB file size capped via `PRAGMA max_page_count`
  - returns structured row results and query plans
  - cacheable = `true`

- **Visual Regression**
  - supports public URLs and artifact/data-URL style inputs
  - SSRF checks on remote URLs
  - returns pixel-diff summary plus annotated PNG artifact
  - cacheable = `false`
  - fixed a real image-diff bug: RGBA `getbbox()` can miss color-only diffs, so comparison now uses RGB analysis

- **Live Endpoint Tester**
  - bounded request count and concurrency
  - SSRF checks
  - returns p50/p95/p99, histogram, status distribution, and sample errors
  - cacheable = `false`

### Verification status

- `python -m py_compile` on all touched Phase 7 files: **passed**
- direct harnesses:
  - DB sandbox SQL execution: **passed**
  - live endpoint tester with mocked HTTP upstream: **passed**
  - visual regression with mocked image fetches: **passed**
- `server` import after builtin registration wiring: **passed**

### What I would build next

If continuing Phase 7, the next best agent is **browser_agent** only if the runtime image is ready for Playwright. If not, **multi_language_executor** is the safer next additive capability.

## Implementation Update: Phase 2.1 / 2.2 and Phase 3.2

## Implementation Update: Phase 2.4 / 2.5

I implemented the pipeline and recipe layer on top of the existing job and payment primitives.

### Landed

- **Phase 2.4 pipelines**
  - new `core/pipelines/` package:
    - `db.py`
    - `resolver.py`
    - `executor.py`
    - `__init__.py`
  - new `migrations/0023_pipelines.sql`
  - pipeline persistence and async run records
  - DAG validation with dependency-order checks
  - `$input.*` and `$node.output.*` input mapping resolution
  - serial pipeline execution with real settlement on each step
  - new API routes:
    - `POST /pipelines`
    - `GET /pipelines`
    - `GET /pipelines/{id}`
    - `POST /pipelines/{id}/run`
    - `GET /pipelines/{id}/runs/{run_id}`

- **Phase 2.5 recipes**
  - new `core/recipes.py`
  - built-in public recipe seeding on startup
  - initial recipe set:
    - `modernize-python`
    - `audit-deps`
    - `review-and-test`
  - new API routes:
    - `GET /recipes`
    - `POST /recipes/{id}/run`
  - new MCP tools:
    - `aztea_run_pipeline`
    - `aztea_run_recipe`

### Verification status

- `python -m py_compile` on touched MCP and recipe test files: **passed**
- `server` import after pipeline and recipe wiring: **passed**
- manual isolated `TestClient` harness for:
  - recipe catalog listing
  - recipe execution
  - run polling
  - final output and step-result resolution
  - builtin execution ordering
  - **passed**

The direct `pytest` invocation issue in this shell remains unresolved, so I still do not have a clean pytest transcript for this slice.

### UI note

I also fixed one concrete frontend issue in the async-job UX:

- jobs in `awaiting_clarification` state now have their own visible filter in [frontend/src/pages/JobsPage.jsx](/Users/aakritigarodia/Desktop/agentmarket/frontend/src/pages/JobsPage.jsx)

Before this, the main jobs view made clarification-blocked work too easy to miss.

## Claude Code Integration Hardening

I then did a Claude-first MCP pass to make sure the new Phase 2 surfaces are actually usable by a coding agent without extra discovery churn or accidental duplicate spend.

### Landed

- added MCP discovery tools:
  - `aztea_list_recipes`
  - `aztea_list_pipelines`
- added MCP polling tools for paid long-running surfaces:
  - `aztea_compare_status`
  - `aztea_pipeline_status`
- updated MCP tool descriptions so:
  - `aztea_compare_agents` explicitly points Claude to `aztea_compare_status`
  - `aztea_run_pipeline` explicitly points Claude to `aztea_pipeline_status`
  - `aztea_run_recipe` explicitly points Claude to `aztea_list_recipes`
- updated timeout notes so Claude is told to poll existing runs instead of accidentally creating new ones
- rewrote [scripts/aztea_claude_md_snippet.md](/Users/aakritigarodia/Desktop/agentmarket/scripts/aztea_claude_md_snippet.md) so the Claude bootstrap guidance now covers:
  - spend control
  - async job lifecycle
  - compare workflow
  - recipes / pipelines
  - the full `allowedTools` set for the Aztea control plane

### Why this mattered

There were two real MCP UX traps before this pass:

1. compare sessions had no dedicated polling tool
2. pipeline / recipe runs had no dedicated polling tool

So if a wait window expired, the natural failure mode for Claude was to call the creation tool again and create a second paid run. That is now fixed at the MCP interface level.

### Verification status

- `python -m py_compile` on the updated MCP files: **passed**
- direct meta-tool catalog harness confirming the Claude control-plane tools are present: **passed**
- direct harness for:
  - `aztea_list_recipes`
  - `aztea_list_pipelines`
  - `aztea_compare_status`
  - `aztea_pipeline_status`
  - **passed**

## Implementation Update: Phase 3 adapter surfaces

I implemented the next cross-platform adapter slice so Claude remains the best surface, while Codex and Gemini are no longer second-class citizens.

### Landed

- new shared adapter builder module:
  - [core/tool_adapters.py](/Users/aakritigarodia/Desktop/agentmarket/core/tool_adapters.py)
- upgraded `GET /openai/tools`
  - now includes Aztea control-plane meta-tools in addition to registry agents
  - retains legacy chat-completions / assistants-style shape
- new `GET /openai/responses-tools`
  - OpenAI Responses API compatible function manifest
- new `GET /codex/tools`
  - alias for the Responses-compatible manifest, intended as the Codex buyer surface
- new `GET /gemini/tools`
  - Gemini function-declarations manifest
- new platform setup snippets:
  - [scripts/aztea_codex_md_snippet.md](/Users/aakritigarodia/Desktop/agentmarket/scripts/aztea_codex_md_snippet.md)
  - [scripts/aztea_gemini_md_snippet.md](/Users/aakritigarodia/Desktop/agentmarket/scripts/aztea_gemini_md_snippet.md)
- documentation updates:
  - [docs/orchestrator-guide.md](/Users/aakritigarodia/Desktop/agentmarket/docs/orchestrator-guide.md)
  - [docs/api-reference.md](/Users/aakritigarodia/Desktop/agentmarket/docs/api-reference.md)

### Why this was the right next move

Before this pass:

- Claude had the strongest Aztea integration because MCP exposed the control plane
- `/openai/tools` was thinner and legacy-shaped
- Gemini had no dedicated tool-manifest surface

So Aztea's cross-platform story existed in principle, but not at the same level of usability.

### Verification status

- `python -m py_compile` on the new adapter module, route shard, and tests: **passed**
- `server` import after route wiring: **passed**
- direct `TestClient` harness for:
  - `GET /codex/tools`
  - `GET /gemini/tools`
  - `GET /openai/tools`
  - control-plane tool presence in all three
  - **passed**

## Buyer-surface smoke suite

I then added real buyer-surface smoke coverage so the cross-platform integration claims are executable instead of aspirational.

### Landed

- new smoke coverage in [tests/integration/test_buyer_surface_smoke.py](/Users/aakritigarodia/Desktop/agentmarket/tests/integration/test_buyer_surface_smoke.py)
- extended adapter unit coverage in [tests/test_tool_adapters.py](/Users/aakritigarodia/Desktop/agentmarket/tests/test_tool_adapters.py)
- adapter manifests now include `tool_lookup`, which maps tool name to:
  - `kind`: `meta_tool` or `registry_agent`
  - `agent_id`: present for registry-backed tools
- Codex and Gemini setup docs now document `tool_lookup`:
  - [scripts/aztea_codex_md_snippet.md](/Users/aakritigarodia/Desktop/agentmarket/scripts/aztea_codex_md_snippet.md)
  - [scripts/aztea_gemini_md_snippet.md](/Users/aakritigarodia/Desktop/agentmarket/scripts/aztea_gemini_md_snippet.md)

### What the smoke suite covers

- **Claude / MCP**
  - live `RegistryBridge` refresh against a running Aztea server
  - `initialize`
  - `tools/list`
  - `tools/call` for `aztea_list_recipes`

- **Codex / OpenAI Responses**
  - fetch `/codex/tools`
  - execute a control-plane tool from the manifest (`aztea_list_recipes`)
  - execute a real registry-backed marketplace tool from the manifest (`python_code_executor`)

- **Gemini**
  - fetch `/gemini/tools`
  - verify function declarations
  - verify the same executable tool mapping via `tool_lookup`

### Verification status

- `python -m py_compile` on the smoke-suite files: **passed**
- unified manual buyer-surface harness:
  - caller registration
  - wallet funding
  - `/codex/tools`
  - `/gemini/tools`
  - live MCP bridge request handling
  - real `python_code_executor` invocation through registry call
  - **passed**

### Important design correction

While building the smoke suite I found a real product gap:

- the non-MCP manifests were descriptive, but they did not give a host runtime enough information to map a returned function name back to an Aztea agent reliably

That is why `tool_lookup` now exists. Without it, the Codex and Gemini surfaces would have remained incomplete in practice.

## Implementation Update: SDK consolidation + client-aware routing

I finished the main Python SDK consolidation work and tightened the registry surfaces that matter most for cross-platform routing.

### Canonical SDK

- `sdks/python/` is now the canonical package
- added/ported:
  - high-level hire/wait/batch helpers
  - `AgentServer`
  - `CallbackReceiver`
  - async client surface
  - legacy-compatible error classes and dataclass models
  - spend-summary helper
- `sdks/python-sdk/README.md` now points users to the canonical package instead of presenting the split as normal
- added focused regression coverage in [tests/test_python_sdk_consolidation.py](/Users/aakritigarodia/Desktop/agentmarket/tests/test_python_sdk_consolidation.py)

### Client-aware registry and privacy signals

- added privacy/compliance fields to agent listings:
  - `pii_safe`
  - `outputs_not_stored`
  - `audit_logged`
  - `region_locked`
- added search filters for those fields on `POST /registry/search`
- added per-client trust breakdowns on registry responses via `by_client`
  - example shape: `{ "claude-code": 91.0, "codex": 84.0 }`
- surfaced those signals in:
  - MCP manifest descriptions
  - `/codex/tools`
  - `/gemini/tools`
  - `/openai/tools`

### UI correction

I found and fixed a real frontend bug while wiring the new metadata:

- [frontend/src/features/agents/AgentCard.jsx](/Users/aakritigarodia/Desktop/agentmarket/frontend/src/features/agents/AgentCard.jsx) was multiplying `trust_score` by 100 even though the backend already returns 0-100

I also added privacy/compliance chips and top client-trust signal to the agent cards so those backend improvements are visible in the buyer UI.

### Verification status

- `python -m py_compile` on touched backend, SDK, and test files: **passed**
- live manual SDK harness:
  - high-level client methods
  - async client wrappers
  - agent-server processing path
  - **passed after normalization fixes**
- live `TestClient` harness for:
  - privacy-tier registration
  - privacy-aware search
  - per-client trust breakdowns
  - Codex tool lookup metadata
  - MCP manifest description enrichment
  - **passed**

I implemented the next two marketplace mechanics and the first cross-platform tagging slice.

### Landed

- **Phase 2.1 cost estimation**
  - new `POST /agents/{id}/estimate`
  - historical call ring stored on agents
  - p50 / p95 latency estimation with confidence bands
  - all-in caller cost estimate uses the same fee math as real charging
  - MCP meta-tool `aztea_estimate_cost`

- **Phase 2.2 result caching**
  - new `agent_result_cache` table
  - canonical payload hashing
  - trust-gated cache writes
  - TTL-bounded cache hits in sync registry calls
  - zero-cost cache-hit responses exposed to callers
  - cache eviction wired into the sweeper

- **Phase 3.2 universal client tagging**
  - new `client_id` persisted on jobs
  - accepted from `X-Aztea-Client`, query param, or explicit request body where applicable
  - `/jobs` and `/jobs/batch` persist it
  - A2A task creation persists it
  - built-in synchronous registry calls persist it when they create an internal job
  - MCP stdio bridge now sends `X-Aztea-Client: claude-code`
  - MCP meta-tools now send the same client header
  - Python SDK now sends `X-Aztea-Client: aztea-python-sdk`
  - frontend now sends `X-Aztea-Client: web-app`

- **Phase 2.3 compare hiring**
  - new compare-session persistence in `core/compare.py`
  - `POST /jobs/compare`
  - `GET /jobs/compare/{id}`
  - `POST /jobs/compare/{id}/select`
  - compare jobs reuse the existing output-verification hold so completed sub-jobs stay unpaid until winner selection
  - selecting a winner accepts and settles only the winning job
  - completed non-winners are fully refunded from the held charge
  - MCP tools:
    - `aztea_compare_agents`
    - `aztea_select_compare_winner`

### Verification status

- manual isolated harness for cost estimation: **passed**
- manual isolated harness for trusted cache hit / no second charge: **passed**
- manual isolated harness for `client_id` persistence on single and batch job creation: **passed**
- manual isolated harness for compare creation, result hold, winner settlement, and non-winner refund: **passed**
- frontend production build after the web client header change: **passed**

### Important note

The integration harness needed one real correction:

- `tests/integration/conftest.py` was not patching the new `core.cache` module onto the isolated test DB

Without that, cache tests would read and write the default DB. That is fixed now.

I audited Claude's claim that Phase 0 and Phase 1 were already done.

The short version:

- **mostly true in substance**
- **not fully true in finish quality**
- **a few important MCP issues still needed fixing**

### What was already there

- `scripts/aztea_mcp_meta_tools.py` existed with the Phase 1 control-plane tools:
  - wallet balance
  - spend summary
  - daily limit
  - top-up URL
  - session budget
  - session summary
  - async hire
  - job status
  - clarify
  - rating
  - dispute
  - verify output
  - discover
  - examples
  - batch hire
- `scripts/aztea_mcp_server.py` already carried MCP session state and surfaced refund metadata in errors.
- `core/mcp_manifest.py` already had the richer description format with trust, success rate, latency, call volume, pricing, and example output.
- The Phase 0 agent fixes were mostly present:
  - `changelog_agent` had GitHub Releases + fallback behavior
  - `pr_reviewer` used `GITHUB_TOKEN` / `GH_TOKEN`
  - `test_generator` already did AST validation and smoke execution

### What was still wrong or incomplete

Before my patch, these problems remained:

1. `aztea_verify_output` posted to the wrong backend route.
   - It used `/jobs/{id}/output-verification-decision`
   - The real route is `/jobs/{id}/verification`

2. Async MCP session spend tracking used the wrong amount.
   - It accrued `price_cents`
   - It should accrue `caller_charge_cents` when present, because that is the caller's real all-in spend

3. MCP meta-tool error shaping was incomplete.
   - Nested refund metadata inside FastAPI `detail.data` was not reliably surfaced
   - That weakened the Phase 1.8 refund/balance UX

4. The HTTP MCP manifest endpoints were still behind the stdio bridge.
   - `scripts/aztea_mcp_server.py` used the richer manifest builder
   - `/mcp/tools` and `/mcp/manifest` in `part_007.py` still used older thin descriptions

5. Some roadmap claims still are not actually verified as complete:
   - I do **not** see the package-finder "embedding fallback over PyPI/npm metadata" described in the roadmap
   - I did **not** verify the sandbox-image `mypy` / `tsc` installation from repo code alone
   - I do **not** see the dedicated end-to-end async MCP lifecycle test Claude described

### What I fixed

- corrected the MCP verify-output route in `scripts/aztea_mcp_meta_tools.py`
- fixed async session spend accrual to prefer `caller_charge_cents`
- improved MCP error parsing so nested refund metadata and wallet balance come through
- fixed `aztea_clarify` so it sends the backend's required `answer` payload and includes `request_message_id`
- aligned HTTP MCP manifest generation with `core.mcp_manifest` so HTTP and stdio now share the same richer description builder
- added regression tests around:
  - nested refund metadata parsing
  - verify-output route selection
  - clarification payload shape
  - async spend accrual
  - richer MCP description content
- added an end-to-end async MCP lifecycle integration test covering:
  - async hire
  - clarification request + response
  - status polling
  - verification
  - rating
  - dispute

### Verification notes

- targeted manual test harness for the new MCP/unit checks: **passed**
- self-contained MCP async lifecycle harness against `TestClient`: **passed**
- `server` import after the manifest patch: **passed**
- `server/application_parts/part_007.py` is back under the 1000-line limit after the refactor
- frontend production build after UI fixes: **passed**

The repo-wide `scripts/check_file_line_budget.py` currently reports many failures from checked-in virtualenv directories under `.release-venv/` and `tui/.release-venv/`. That is not caused by this patch, but it means the current line-budget script output is noisy unless those directories are excluded or removed.

### Updated judgment

Claude was broadly right that Phase 0 and Phase 1 work had been done.

But the more accurate statement is:

- **Phase 0: mostly done, with some claims stronger than the current code evidence**
- **Phase 1: implemented enough to demo, but not fully tightened**
- **not ready to call "done" without sharper integration coverage**

That changes what should happen next.

## What GPT-5.4 should do next

The next few tasks should stay in the coding-agent niche and finish the control plane before adding new marketplace mechanics.

### 1. Add real end-to-end MCP lifecycle tests

Highest priority.

Specifically:

- async hire
- job status polling
- progress / partial-result surfacing
- clarification request + clarify response
- rating
- dispute
- verification decision
- refund metadata in MCP error responses

This is the fastest path to taking Phase 1 from "feature-complete demo" to "credible product surface".

### 2. Audit and finish the remaining Phase 0 credibility gaps

Most important open items:

- verify whether `package_finder` really needs the roadmap's fallback or whether the existing LLM + registry logic is sufficient
- verify sandbox support for `mypy` and TypeScript paths with an actual execution test, not just code inspection
- add a direct regression test for refund-on-failure invariants on MCP-facing builtin agents

### 3. Add cost estimation before heavier orchestration features

If you want one user-facing feature next, make it:

- `POST /agents/{id}/estimate`
- MCP `aztea_estimate_cost`

Reason:

- directly useful for coding agents
- low conceptual risk
- improves spend transparency before pipelines, compare sessions, and caching
- can be implemented cleanly in a new helper module without pushing more logic into already-overbudget files

### 4. Then add result caching

Caching is the next best Phase 2 feature after cost estimation because it:

- helps iterative coding workflows immediately
- is legible to users
- has bounded settlement risk if trust-gated and TTL-bounded

### 5. Defer the financially trickier mechanics

Do **not** prioritize these before the above:

- payout curves
- SLA refund overlays
- subscriptions
- team wallets

Those are product-valid ideas, but they are not the highest-leverage work for your current wedge.

I agree with the product direction:

- cost estimation
- caching
- compare mode
- pipelines/recipes
- SLA-aware contracting
- versioning/rollback

I do **not** agree with treating all of that as one implementation phase if the target standard is `A` or `A+`.

That prompt is good as a feature ideation document. It is not yet a strong execution plan for a startup codebase that already has real money, escrow, settlement, disputes, and multiple client surfaces.

If you execute it literally, the likely outcome is:

- lots of feature surface shipped
- core invariants stressed across too many paths
- payment logic duplicated in more places
- route shards growing faster than the domain model
- quality moving from `B+/A- backend with strong bones` to `feature-rich but harder to trust`

If the goal is to take Aztea to `A/A+`, the next phase should be about **tightening the product thesis and hardening the architecture around it**, not just adding more mechanics.

---

## What "A/A+" means for Aztea

For this repo, `A/A+` is not "more features". It means:

1. The money paths are obviously correct.
2. New marketplace mechanics compose with existing jobs/ledger/dispute logic instead of bypassing it.
3. The coding-agent niche is first-class in the API, MCP, and UX.
4. The platform has a smaller number of stronger primitives rather than many partially-overlapping ones.
5. The developer surfaces feel opinionated and excellent, especially for coding CLI users.

That changes the recommendation set quite a bit.

---

## My read on Claude's Phase 2 prompt

### What Claude got right

- The feature choices are mostly coherent with marketplace mechanics.
- The prompt is very explicit about invariants.
- It respects the repo's current architecture.
- It pushes toward differentiation beyond a plain agent registry.
- Pipelines + recipes + versioning + SLA all point toward "serious work marketplace", not toy demos.

### What I disagree with

#### 1. The phase is over-scoped

Eight major features in one phase is too much for this codebase if quality is the standard.

The risky ones are:

- comparative hiring
- pipelines
- payout curves
- SLA refunds
- versioning/rollback

Each one touches settlement semantics or routing behavior. That is not "one feature"; that is a new contract layer.

#### 2. The sequencing is not architecture-first enough

The prompt adds new mechanics before consolidating the domain seams:

- call execution
- pre-charge vs final settlement
- refund/clawback logic
- job outcome state transitions
- sync call vs async job behavior

Right now those seams are workable, but they are spread across `core/*` and `server/application_parts/*`. If you add Phase 2 directly on top, you'll increase policy duplication.

#### 3. It underweights the coding-CLI niche

The prompt is marketplace-general. Your stated niche is narrower and stronger:

- coding agents using CLI tooling
- delegation across coding sub-agents
- compare/review/test/fix flows
- reproducible outputs
- repo-aware orchestration

That niche should drive the design of Phase 2. Right now it does not.

#### 4. It mixes "great product ideas" with "dangerous financial semantics"

Examples:

- compare sessions
- payout-curve clawbacks after ratings
- SLA refunds

Those are interesting, but they should only ship after the settlement engine is pulled into a smaller set of reusable primitives. Otherwise you will add exception logic in too many places.

---

## The actual recommendations to get to A/A+

## 1. Re-scope Phase 2 into two tracks

Do not treat this as one phase.

Split it into:

- **Phase 2A: marketplace primitives**
- **Phase 2B: advanced financial/mechanical overlays**

### Phase 2A should include

- Cost Estimation API
- Result Caching
- Compare Hiring
- Pipelines
- Recipes

These directly improve user value and discovery without inventing too many new settlement semantics.

### Phase 2B should include

- Payout Curves
- SLA Contracts
- Agent Versioning + Auto-Rollback

These are good ideas, but they are policy-heavy and should come after refactoring shared settlement/orchestration logic.

Recommendation: do not start 2.6, 2.7, or 2.8 until the execution and settlement paths are cleaner.

---

## 2. Extract a single "call execution" domain before adding more routes

This is the highest-leverage engineering change in the repo.

Today, the logic for:

- estimate
- pre-charge
- invoke
- settle
- refund
- verify
- cache write
- SLA adjustment
- compare-session usage
- pipeline-step usage

is too easy to scatter.

Create a new core domain package before adding most of Phase 2:

- `core/execution/__init__.py`
- `core/execution/planner.py`
- `core/execution/runner.py`
- `core/execution/settlement.py`
- `core/execution/types.py`

That package should own:

- sync call lifecycle
- async job creation helpers
- pricing resolution
- cache lookup/write hooks
- compare/pipeline reuse
- post-success adjustment hooks

If you do this first, most Phase 2 work gets easier and safer.

Without it, Phase 2 will likely work but feel increasingly improvised.

Grade impact: this one change is the shortest path from `A- backend` to `A backend`.

---

## 3. Make coding-CLI support a first-class product axis

This is the biggest product recommendation.

If your niche is "Aztea is the best marketplace and routing layer for coding agents working through CLIs", then the platform needs coding-native primitives, not just generic task primitives.

### Add a "coding task protocol"

Instead of relying only on loose `input_payload`, define a normalized protocol envelope for coding work:

- repo URL or attached repo artifact
- branch/ref
- task type: `review`, `fix`, `test`, `audit`, `modernize`, `compare`
- expected output mode: `patch`, `findings`, `plan`, `tests`, `diff_summary`
- tool permissions / sandbox expectation
- runtime hints

You already have protocol-related work in jobs. Push it further for coding.

### Add coding-specific MCP meta-tools

Examples:

- `aztea_compare_code_reviewers`
- `aztea_run_fix_and_test`
- `aztea_audit_repo`
- `aztea_generate_patch`
- `aztea_delegate_repo_task`

These should not just proxy generic API calls. They should encode good defaults for coding workflows.

### Add coding recipes first

If you only seed three public recipes, they should clearly target the coding niche:

- review-and-test
- audit-deps
- modernize-python

Claude already suggested these. I agree with that part strongly.

### Add richer result artifacts for coding agents

To win coding CLI users, result payloads need better structure:

- patch or diff artifact
- changed files
- test summary
- linter summary
- confidence / residual risk
- execution transcript excerpt

This matters more than adding a generic public recipe browser.

---

## 4. Treat "compare hiring" as a flagship feature, but simplify the economics

I agree with the feature. I would simplify the first version.

### Keep

- 2-3 agents
- same input
- poll combined status
- explicit winner selection

### Simplify

First version should use:

- pre-charge all
- settle winner
- refund losers

Do **not** add participation fees yet.
Do **not** add complex partial settlement semantics yet.
Do **not** let compare mode recursively compose with every other mechanic on day one.

### Why

Compare mode is ideal for coding agents:

- compare two code reviewers
- compare two fixers
- compare two test generators
- compare two dependency auditors

This is a real differentiator for CLI agents. But it only stays good if the settlement logic remains boring and obvious.

---

## 5. Build pipelines for determinism, not just DAG flexibility

I agree with pipelines, but the first version should be narrower than the prompt suggests.

### Recommended v1 constraints

- serial execution only
- no branching joins beyond simple dependency order
- no cycles
- no dynamic fan-out
- strict input mapping resolver
- explicit max step count
- explicit max cumulative budget

### Why

Your niche is coding agents. Those users care more about:

- reproducibility
- inspectability
- debuggability

than arbitrary DAG expressiveness.

The best v1 pipeline is one you can explain in one screen and debug from one job timeline.

### What to add for A/A+

For each pipeline run, store:

- node execution order
- per-node input snapshot
- per-node output snapshot
- cost per node
- latency per node
- failure node and reason

That turns pipelines from "hidden orchestration" into "auditable orchestration". That is much closer to A+.

---

## 6. Prioritize observability for new mechanics as part of feature definition

Right now new features are described mostly in terms of routes and tables. That is not enough for A/A+.

Every new mechanic should ship with metrics and operator visibility.

### Add metrics for

- compare session created/completed/refunded
- cache hits/misses by agent
- pipeline runs created/completed/failed
- estimate endpoint usage
- rollback triggers
- SLA refunds applied

### Add operator/debug endpoints or structured logs for

- compare settlement decisions
- cache writes and evictions
- pipeline step traces
- version rollback reasons

If a mechanic affects money or routing and you cannot explain what happened from logs and metrics, it is not A quality.

---

## 7. Introduce a clear "post-settlement adjustment" primitive

This is required before payout curves and SLA refunds.

Right now those ideas require compensating ledger actions after the original job settles.

That is fine, but it should be standardized.

Create a shared adjustment primitive with typed reasons, for example:

- `quality_adjustment`
- `sla_adjustment`
- `compare_loser_refund`
- `version_rollback_credit` if ever needed later

Put this in `core/payments` and make the server use it instead of one-off clawback/refund code paths.

This will:

- reduce money-path duplication
- make reconciliation easier
- make admin debugging easier
- make tests more systematic

Do this before payout curves and SLA.

---

## 8. Do not ship payout curves until the rating model is stronger

I like the concept. I do not recommend shipping it yet.

Why:

- it couples payout directly to post-hoc subjective ratings
- ratings are sparse and noisy
- it creates a new adversarial surface
- it invites gaming and retaliation behavior

In a human marketplace this can work with enough volume and moderation. In an agent marketplace, especially early, it is likely too noisy.

Recommendation:

- keep the design note
- do not prioritize implementation
- revisit after stronger acceptance/verification signals exist

If you really want to keep the idea alive, convert it into:

- opt-in escrow holdback
- release based on explicit acceptance or verifier pass

That is a stronger primitive than rating-based clawback.

---

## 9. Ship SLA contracts only for latency first

I agree with SLAs. I disagree with mixing latency and trust in the first version.

### Good v1 SLA

- caller specifies `max_latency_ms`
- if actual latency exceeds it, partial refund applies automatically

### Avoid in v1

- `min_trust_score` as a contractual SLA term

Why:

- trust score is a platform-level heuristic, not a concrete execution guarantee
- tying refunds to trust score makes the economics less intuitive
- it invites confusion when trust moves over time

Latency is objective. Start there.

Then, if needed later, add separate "routing constraints" for trust rather than a financial SLA on trust.

---

## 10. Agent versioning is good, but rollout control matters more than rollback

I agree with versioning and rollback, but I would change emphasis.

First ship:

- publish version
- list versions
- activate version
- caller pin by version

Then ship:

- progressive rollout percentage
- canary traffic
- auto-rollback

Why:

- rollback without rollout control is reactive only
- coding agents benefit a lot from version pinning and reproducibility
- caller-side version pinning is already very valuable for CLI workflows

For your niche, "run against version `1.2.3` and give me a reproducible result" is more important than automated rollback on day one.

---

## 11. Add frontend and UX support only where it sharpens the niche

The frontend is currently the least A-grade part of the stack relative to the backend.

To improve it:

### Do

- build a dedicated "coding workflows" surface under recipes/pipelines
- show compare results in a side-by-side diffable layout
- show pipeline step traces clearly
- expose cache hits, estimates, and version pins in job detail

### Do not over-invest yet in

- broad marketplace chrome
- too many generic dashboards
- decorative UX work that does not support the workflow

The UI should help users answer:

- Which coding agent should I use?
- What did each one produce?
- What did I pay?
- Can I replay this workflow?
- Can I pin the version that worked?

That would move the frontend from `B-` toward `B+/A-`.

---

## 12. Add browser-level frontend testing before claiming A/A+

This is mandatory.

The backend has much better test posture than the frontend.

Add Playwright coverage for:

- signup/login
- browse agents
- hire agent
- compare agents
- run recipe
- view pipeline run
- top-up / wallet happy path

Without browser-level tests, the product as a whole will not be A-grade even if the backend is.

---

## 13. Strengthen the MCP surface into a true "control plane"

This is where your startup can differentiate hard.

The MCP bridge should not just expose tools. It should feel like the native control plane for coding agents.

### Recommendations

- session budget should be first-class and always visible
- compare, recipe, pipeline, and estimate tools should be deeply ergonomic
- tool descriptions should include coding-specific examples
- responses should return structured machine-friendly summaries, not just text
- job status / clarification / verification flows should be easy to script from coding agents

Most importantly:

### Add "delegation ergonomics"

Examples:

- "hire this agent asynchronously and return a handle"
- "compare these two and summarize differences"
- "run this recipe against this repo and return changed files"
- "resume polling this long-running job"

That is stronger than just exposing raw marketplace actions.

---

## 14. Add a canonical "repo task" input schema for coding work

If you want coding CLI integration to be the niche, then repo-aware job inputs should stop being ad hoc.

Add a documented schema for repo tasks, for example:

- `repo_url`
- `ref`
- `subdir`
- `task`
- `language`
- `artifacts`
- `expected_output`
- `constraints`

Use it in:

- compare mode
- recipes
- pipeline runs
- agent examples
- MCP tool examples

This gives Aztea a real product identity for coding work instead of just "generic JSON task marketplace."

---

## 15. Recommended execution order

If the goal is A/A+, this is the order I would actually use.

### Step 1 — architecture hardening

- extract shared execution domain
- add standardized post-settlement adjustment primitive
- add browser-level frontend test harness

### Step 2 — high-value marketplace primitives

- cost estimation
- result caching
- compare hiring
- version publish/list/activate/pin

### Step 3 — coding niche productization

- coding recipes
- serial pipelines for repo work
- coding-specific MCP tools
- canonical repo-task protocol

### Step 4 — advanced overlays

- latency-only SLA refunds
- version canary/rollback

### Step 5 — reconsider later

- payout curves
- trust-based SLA economics

---

## What I would tell Claude directly

I would tell Claude:

"The feature list is strong, but the phase plan is too broad and too eager to add new financial semantics. Reframe it around a shared execution core, prioritize coding-agent workflows explicitly, ship compare/pipelines/recipes/version pinning first, and defer payout curves plus trust-based SLA mechanics until the settlement engine is more centralized."

That is the version I agree with.

---

## Final recommendation

Yes, use Claude's prompt as a source document.

No, do not execute it literally.

If the real target is `A/A+` and your wedge is coding CLI integrations, then the best move is:

- narrow the scope
- centralize execution/settlement logic
- make coding workflows first-class
- treat MCP as a native control plane
- defer the noisiest economic mechanics until the core is cleaner

That would improve both product clarity and code quality at the same time.

---

## Update after reviewing Claude's full roadmap

After reading the full roadmap, my view changes in one important way:

I agree with the overall structure much more than I agreed with the isolated Phase 2 prompt.

Why:

- Phase 0 is the right instinct: credibility before expansion.
- Phase 1 is much stronger than the original Phase 2-first framing.
- Phase 3 is strategically correct for your startup wedge.
- The roadmap correctly identifies the gap between Aztea's backend depth and what coding agents can actually reach today.

So the updated judgment is:

- **I agree with the roadmap thesis.**
- **I do not agree with the implementation order inside the later phases unless Phase 0 and the best parts of Phase 1 land first and cleanly.**

### What I now agree with strongly

#### 1. Phase 0 is non-negotiable

Claude is right that broken tools are credibility killers.

If the exposed tools are unreliable, then:

- richer metadata does not matter
- async orchestration does not matter
- pipelines do not matter
- cross-platform expansion does not matter

The marketplace has to be trustworthy before it can be impressive.

#### 2. Phase 1 is the real wedge

This is the most important part of the full roadmap.

The hidden 95% argument is correct. Aztea already has:

- async jobs
- clarifications
- ratings
- disputes
- SSE
- webhooks
- batch work
- verification
- trust
- wallet logic

and almost none of that is meaningfully reachable from coding agents today.

Closing that gap is much higher leverage than adding new marketplace economics.

#### 3. The clarification protocol is genuinely differentiated

Claude is right about this.

If Aztea lets coding agents:

- hire asynchronously
- observe progress
- receive clarification requests
- answer them in-band
- continue execution

then it becomes much more than a static tool registry.

That is a real product moat, especially for coding-agent workflows.

#### 4. Phase 3 is strategically important

The cross-platform ambition is correct.

If you want Aztea to be the layer underneath coding agents generally, then:

- Claude Code cannot be the only buyer surface
- MCP cannot be the only integration story
- SDK duplication needs to be cleaned up
- client identity should be explicit early

I would still avoid spreading too thin too early, but the direction is right.

---

## Revised view of the roadmap by phase

### Phase 0 — Agree almost completely

This should ship first.

I agree with:

- broken tool fixes
- universal refund/failure contract
- tool description cleanup
- logic-validation pass for `test_generator`

One addition:

- include a small, deterministic smoke-test harness for the exposed MCP tool set, so "credibility" is continuously checked rather than manually re-proven.

### Phase 1 — Highest priority phase in the whole roadmap

This is where I would put the most energy.

I agree strongly with:

- enriched tool descriptions
- wallet + budget meta-tools
- async hire/status/clarify
- rating/dispute/verification exposure
- discovery/search exposure
- batch exposure
- refund status in MCP errors

I would refine two things:

#### 1. Build the MCP meta-tool layer as a control-plane module, not ad hoc bridge logic

Do not let `scripts/aztea_mcp_server.py` become the only place where business behavior lives.

Create a reusable layer, for example:

- `server/mcp_meta_tools.py` or `core/mcp_control_plane.py`

so that:

- tool definitions
- request shaping
- response shaping
- session budget state
- auth-required behavior

are not smeared across one script.

#### 2. Do the async lifecycle in a way that preserves structured outputs

Do not make `aztea_job_status` a text-first status endpoint.

It should return machine-friendly fields for:

- current status
- recent progress messages
- clarification request payloads
- partial results
- output verification state
- refund state if failed

That will matter a lot for coding-agent integrations.

### Phase 2 — Good ideas, but should be split

My earlier view still holds.

Split Phase 2 into:

- **Phase 2A: execution/product primitives**
- **Phase 2B: financial and policy overlays**

#### Keep in Phase 2A

- cost estimation
- result caching
- compare hiring
- pipelines
- recipes
- agent version publish/list/activate/pin

#### Move to later or narrow heavily

- performance bonds / payout curves
- trust-based SLA economics
- auto-rollback tied to financial routing changes
- privacy/compliance tiers unless you already have active enterprise demand
- team wallets unless there is actual sales pull
- subscriptions unless the current top-up flow is measurably blocking usage

### Phase 3 — Good strategy, but sequence carefully

I agree with:

- client identification
- SDK consolidation
- per-platform docs
- OpenAI/Codex/Gemini adapter expansion

But I would not begin by trying to support every platform evenly.

Recommended order:

1. Claude Code MCP
2. Codex / OpenAI function-calling wrapper
3. Cursor validation
4. Gemini adapter
5. Everything else

Reason:

- Claude Code is the current proving ground
- Codex is the next most important adjacent surface
- the rest should follow only once the control-plane abstractions are stable

### Phase 4 — Reasonable, but not near-term

The ideas are good, but this phase should stay parked until there is real usage.

Especially:

- proactive suggestions
- subcontracting
- coding agent as worker
- federated reputation

These depend on actual volume and behavioral data.

---

## What matters most for A/A+ after seeing the full plan

The most important insight from Claude's full roadmap is this:

Aztea does not primarily need "more platform depth."

It already has platform depth.

It needs:

1. **exposure of existing depth**
2. **excellent coding-agent ergonomics**
3. **cleaner execution/control-plane abstractions**
4. **better reliability guarantees on exposed tools**

That is why the roadmap should be evaluated less like "which feature is coolest?" and more like "which changes make Aztea feel inevitable to a coding agent?"

---

## Best first implementation tranche for GPT-5.4

If I were assigning the first work to GPT-5.4, I would not start with pipelines, compare sessions, subscriptions, or team wallets.

I would start with the smallest set of changes that:

- materially improve credibility
- directly sharpen the coding-agent wedge
- mostly reuse existing backend capability
- keep financial risk low

### Tranche 1 — Credibility pack

This is the best first use of GPT-5.4.

Scope:

- fix the broken built-in tools
- enforce and test the universal refund-on-failure contract
- clean up built-in tool descriptions
- harden `test_generator` with smoke-run validation

Why this first:

- bounded scope
- high user-visible impact
- low product ambiguity
- low risk of architectural sprawl
- ideal fit for GPT-5.4's coding style: focused bug-fix and medium-sized integration work

Success condition:

- all currently exposed tools can survive a smoke pass
- failures clearly return zero-cost / refunded state
- no more credibility gap on day one

### Tranche 2 — MCP manifest and error enrichment

This is the second-best use of GPT-5.4.

Scope:

- enrich MCP tool descriptions with trust, price, latency, examples, verification badges
- surface refund/balance/session context in MCP errors
- make session budget and summary state explicit in the bridge

Why second:

- directly improves agent routing behavior
- low risk compared to new settlement mechanics
- creates immediate differentiation for coding agents
- mostly wiring and shaping, not inventing new backend contracts

This tranche is particularly strong because it upgrades decision quality before changing execution behavior.

### Tranche 3 — Wallet and control-plane meta-tools

Scope:

- `aztea_wallet_balance`
- `aztea_spend_summary`
- `aztea_set_daily_limit`
- `aztea_set_session_budget`
- `aztea_topup_url`
- `aztea_session_summary`

Why third:

- essential for real agent autonomy
- still mostly exposing existing primitives
- low core-ledger risk if kept as thin wrappers

This is where Aztea starts feeling like a serious operating layer rather than just a marketplace catalog.

### Tranche 4 — Async MCP lifecycle with clarify/status

This is the highest upside tranche, but I would make it the fourth, not the first.

Scope:

- `aztea_hire_async`
- `aztea_job_status`
- `aztea_clarify`
- structured handling of progress, partial results, and clarification requests

Why not first:

- it is more novel and more integration-heavy
- it touches more moving parts
- it benefits from better manifest/error/control-plane groundwork already being in place

Why still early:

- this is probably the strongest product differentiator in the roadmap
- it fits the coding-agent niche extremely well

### Tranche 5 — Discovery and examples

Scope:

- `aztea_discover`
- `aztea_get_examples`
- ranking/filter polish

Why here:

- useful, but less foundational than credibility, wallet visibility, and async control
- stronger once the richer metadata system is already live

---

## The best few things to implement first

If I had to pick just **three** implementation targets to start immediately with GPT-5.4, they would be:

### 1. Phase 0 credibility pack

This is the clear first move.

It protects trust, improves current exposed behavior, and is the least likely to create architectural debt.

### 2. Phase 1.1 + 1.8 together

Combine:

- enriched MCP descriptions
- enriched MCP error/refund visibility

Reason:

- they are tightly related
- they improve routing and trust immediately
- they make the catalog meaningfully smarter for coding agents

### 3. Phase 1.2 wallet/budget tools

Reason:

- they unlock autonomous cost-aware behavior
- they are practical and differentiating
- they reuse existing backend capabilities almost entirely

If you want a fourth immediately after those, it should be:

### 4. Phase 1.3 async hire + job status + clarify

This is the first "gamechanger" feature, but it should come after the control plane feels reliable.

---

## What I would not give GPT-5.4 first

I would not start GPT-5.4 on these as the first tranche:

- pipelines
- subscriptions
- team wallets
- privacy/compliance tiers
- payout curves
- trust-based SLA mechanics
- full auto-rollback system
- broad multi-platform adapter matrix

These are either:

- too architecture-heavy
- too policy-heavy
- too dependent on earlier control-plane work
- or too far from the current highest-leverage wedge

---

## Recommended revised execution order

If I were running this roadmap now, I would do it in this order:

### Step 0

- Phase 0 credibility pack

### Step 1

- Phase 1.1 manifest enrichment
- Phase 1.8 MCP error enrichment

### Step 2

- Phase 1.2 wallet/budget meta-tools

### Step 3

- Phase 1.3 async hire/status/clarify

### Step 4

- Phase 1.4 rating/dispute/verification
- Phase 1.5 discovery/examples
- Phase 1.6 batch exposure

### Step 5

- internal extraction of shared execution/control-plane abstractions

### Step 6

- Phase 2A only: estimate, caching, compare, recipes, serial pipelines, version pinning

### Step 7

- selected Phase 3 work: Codex/OpenAI adapter, SDK consolidation, client_id tagging

### Step 8

- only then revisit Phase 2B and the heavier economics

---

## Final updated position

After reading the full roadmap, my view is:

- Claude is directionally much more right than the earlier narrow Phase 2 prompt suggested.
- The strongest parts of the roadmap are Phase 0, most of Phase 1, and the narrower parts of Phase 3.
- The best immediate work is not the fanciest new marketplace mechanic. It is the control-plane and reliability work that makes Aztea obviously useful to coding agents.

So the first GPT-5.4 work should be:

1. credibility
2. manifest + error enrichment
3. wallet/budget control plane
4. async clarify lifecycle

That is the most defensible path to both product traction and `A/A+` engineering quality.

---

## Audit update: hardening plan phases 0-4

I audited Claude's newer "End-to-End Hardening, Cleanup, Performance & DX Overhaul" claims against the repo.

### What was already genuinely present

- feature flags and lightweight observability exist:
  - `core/feature_flags.py`
  - `core/observability.py`
- the settlement gate fix is present in `server/application_parts/part_005.py`
- the semantic-search fallback is materially improved in `core/embeddings.py`
- the curated builtin cleanup is present in `server/builtin_agents/constants.py`
- structured pipeline create/run routes and shorthand definition acceptance are present
- the broken-agent repair work had started:
  - `cve_lookup` already had an NVD-first path
  - `requirements.txt` already included `mypy`

### Holes I found and fixed

#### 1. Sync remote calls were still not fully on the new envelope contract

Builtins and hosted skills already created real sync jobs, but remote sync calls still returned:

- `job_id: null`
- no persisted sync job record for successful remote calls
- idempotent replay of the raw output shape instead of the wrapped envelope

I fixed that in [server/application_parts/part_008.py](/Users/aakritigarodia/Desktop/agentmarket/server/application_parts/part_008.py):

- remote sync calls now create a real job before dispatch
- successful remote sync calls now return the standard envelope:
  - `job_id`
  - `status`
  - `output`
  - `latency_ms`
  - `cached`
- remote failures now mark the job failed and flow through the same settlement/refund machinery
- idempotent replay now returns the full envelope, not the raw agent payload

This was the highest-signal buyer-surface hole from the audit.

#### 2. `type_checker` was still not at the "real tool" bar

I rewrote [agents/type_checker.py](/Users/aakritigarodia/Desktop/agentmarket/agents/type_checker.py) so it:

- runs `python3 -m mypy`
- requests JSON diagnostics
- writes a minimal temporary `mypy.ini`
- returns structured `diagnostics`
- keeps compatibility fields like `passed`, `errors`, and `error_count`

This removes the old "tool exists but behaves like a thin wrapper" problem.

#### 3. `linter_agent` still had an LLM fallback

I rewrote [agents/linter_agent.py](/Users/aakritigarodia/Desktop/agentmarket/agents/linter_agent.py) so it is now tool-first:

- Python uses `ruff`
- JS/TS use `eslint` via `npx` when available
- if the runtime cannot support JS/TS linting, it returns a structured `tool_unavailable` error
- it no longer silently degrades to an LLM review path

That matches the intended marketplace standard much better.

#### 4. `cve_lookup` needed the fallback path finished

[agents/cve_lookup.py](/Users/aakritigarodia/Desktop/agentmarket/agents/cve_lookup.py) already had most of the NVD-first conversion, but the direct CVE-ID path still needed the fallback behavior completed.

I finished that by adding:

- NVD-first direct lookup
- OSV fallback for direct CVE-ID lookups when NVD is unreachable
- explicit `source` tagging for the returned result

### Regression coverage added

- new integration coverage:
  - [tests/integration/test_sync_call_envelope.py](/Users/aakritigarodia/Desktop/agentmarket/tests/integration/test_sync_call_envelope.py)
- new unit coverage:
  - [tests/test_agent_real_tool.py](/Users/aakritigarodia/Desktop/agentmarket/tests/test_agent_real_tool.py)

These cover:

- remote sync envelope shape + idempotent replay
- `cve_lookup` NVD preference and OSV fallback
- `type_checker` structured mypy parsing
- `linter_agent` tool-unavailable behavior for JS and real `ruff` parsing for Python

### Verification notes

What I verified directly:

- `python -m py_compile` on all touched backend and test files: passed
- `server` import: passed
- direct harness for the repaired real-tool agents: passed

What remains awkward:

- direct `pytest` invocation from this shell still exits without usable output
- `python scripts/check_file_line_budget.py` is globally red, but not because of these edits; the repo already contains many files over the historical 1000-line target

### Practical conclusion

Claude had a meaningful amount of the hardening plan in place, but it was not correct to treat phases 0-4 as "done."

The most important real gaps were:

- the remote sync job/envelope path
- the linter/tooling realism bar
- the missing regression coverage for the repaired agents

Those are now materially tighter.

---

## Implementation update: Phase 5 performance work

I then moved into Phase 5. The important distinction here is:

- some of Phase 5 already existed in rough form
- most of the buyer-facing token/latency wins did not

### What was already there

- async job SSE streaming already existed at `/jobs/{id}/stream`
- built-in `internal://...` agent execution was already direct in several hot paths

So not every item in Phase 5 needed a net-new implementation.

### What I added

#### 5.1 Output truncation

New module:

- [core/output_shaping.py](/Users/aakritigarodia/Desktop/agentmarket/core/output_shaping.py)

This now provides summary-vs-full shaping for large outputs:

- strings over ~2 KB get truncated
- lists over 50 items get clipped with a truncation marker
- large base64-like blobs are replaced by an artifact placeholder

Wiring:

- sync registry call envelopes now shape `output` in summary mode
- job responses now shape `output_payload` by default
- new full-output route:
  - [server/application_parts/part_009.py](/Users/aakritigarodia/Desktop/agentmarket/server/application_parts/part_009.py)
  - `GET /jobs/{job_id}/full`

The practical effect is that coding-agent buyers no longer need to absorb giant blobs just to inspect whether a job succeeded.

#### 5.2 Result cache v2 normalization

Updated:

- [core/cache.py](/Users/aakritigarodia/Desktop/agentmarket/core/cache.py)

Changes:

- cache keys now incorporate a version identity token, not just `(agent_id, payload)`
- non-cacheable internal executors are explicitly opted out
- cached payloads retain `_cached_job_id`
- cache-hit sync responses now expose `original_job_id`

Wiring:

- [server/application_parts/part_008.py](/Users/aakritigarodia/Desktop/agentmarket/server/application_parts/part_008.py)

This is materially closer to a real cache layer instead of "same payload, same agent, trust me."

#### 5.3 Lazy MCP schema loading

Updated:

- [scripts/aztea_mcp_server.py](/Users/aakritigarodia/Desktop/agentmarket/scripts/aztea_mcp_server.py)

Added a slim MCP mode behind `AZTEA_LAZY_MCP_SCHEMAS`:

- `aztea_search`
- `aztea_describe`
- `aztea_call`

This keeps the stdio tool list tiny and defers full schema expansion until the model actually needs it.

Important detail:

- I left this behind the flag instead of flipping it on unconditionally
- that matches the rollout discipline better and avoids breaking the existing direct-tool MCP expectations in one jump

For Claude specifically, this is the most important remaining token-saving switch in the repo.

#### 5.4 Python executor warm pool

Updated:

- [agents/python_executor.py](/Users/aakritigarodia/Desktop/agentmarket/agents/python_executor.py)

Added a warm-pool execution path behind `AZTEA_PYTHON_WARM_POOL`:

- uses a multiprocessing pool
- executes code in a pre-forked worker process
- resets the pool on timeout/error
- keeps the old subprocess path as the default fallback

This is intentionally gated. The pool is there now, but it is not forced on by default.

#### 5.6 Fast-path extraction

New module:

- [core/fastpath.py](/Users/aakritigarodia/Desktop/agentmarket/core/fastpath.py)

And I wired it into:

- [core/pipelines/executor.py](/Users/aakritigarodia/Desktop/agentmarket/core/pipelines/executor.py)

This does not create a brand-new capability so much as formalize the same-process dispatch path for:

- hosted skills
- internal builtins

That makes the low-latency local path explicit and reusable instead of having it inlined in every caller.

### Verification status

Verified directly:

- `python -m py_compile` on all touched Phase 5 files: passed
- direct output-shaping harness: passed
- direct lazy-MCP harness: passed
- direct cache-v2 identity/cacheable harness: passed
- direct Python warm-pool harness: passed

### Remaining Phase 5 judgment

The main practical Phase 5 items are now in place:

- output shaping
- cache normalization
- lazy MCP
- Python warm pool
- explicit local fast-path helper

The one item that was already materially present before this pass was async SSE streaming.
