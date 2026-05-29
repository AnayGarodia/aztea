# Changelog

All notable changes to Aztea are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Aztea follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-05-29

Auto-hire ranker overhaul. 12 ranker improvements + reflex/eval foundation,
all behind feature flags (defaults preserve current behavior). Multi-layered
defense against LLM cost amplification, Sybil ranking sabotage, and prompt
injection.

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
