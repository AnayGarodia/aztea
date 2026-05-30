# Aztea Full Codebase Review — 2026-05-30

> **Report-only review.** No production source was modified. Diagnostic tests under
> `tests/review_findings/` demonstrate bugs; they do not fix them.
> Scope: risk-prioritized (exhaustive on money/auth/security + all fresh v1.2.1 code).
> Plan: `~/.claude/plans/goofy-whistling-grove.md`. Baseline commit: `085fc45c` (v1.2.1).

## Remediation status (branch `fix/security-review-2026-05-30`, worktree `aztea-review-fixes`)

Fixes landed on a separate worktree, each with tests + flake8, smallest-risk first.

**Branch verification (post-fix):**
- Full unit suite (`tests`, minus sdk_contract + integration): **green except one environmental
  meta-test** — `test_oss_audit_extras.py::test_make_oss_check_runs_clean` fails because the
  worktree has no `.venv` so the Makefile falls back to bare `python` (absent on this host;
  only `python3`/venv exist). It fails identically with zero edits; passes on `main` only
  because `main` has a `.venv`. The actual oss-check *work* passes: OSS isolation 8/8,
  no hardcoded `aztea.ai` URLs, line-budget unaffected. Not caused by any code change.
- `flake8` clean across all 15 changed `.py` files.
- Targeted per-fix suites all green (auth/OTP, listing_safety+onboarding, llm/provider/fallback/byok,
  llm-budget, pipeline/workspace/outbound, auto_hire/hosted_index). Frontend build ✓ + vitest 89/89 ✓.
- Every new diagnostic test verified **failing on `main`, passing on the branch**.

| Finding | Status | Commit |
|---|---|---|
| **B-S5** OTP non-crypto random | ✅ FIXED (`secrets.randbelow`) | crypto-secure OTPs |
| **B-S6** Greek-homoglyph endpoint bypass | ✅ FIXED + regression test | homoglyph fold / test |
| **B-S7** BYOK key copied into os.environ | ✅ FIXED (use original env var) | llm providers commit |
| **A-C4** LLM providers leak raw exceptions | ✅ FIXED (3 providers) + tests | llm providers commit |
| **A-C6** _llm_budget data races | ✅ FIXED (locks) + stress tests | llm-budget commit |
| **A-C8** WalletPage poll leak | ✅ FIXED (effect cleanup) | frontend commit |
| **B-S4** pipeline DNS-rebind SSRF | ✅ FIXED (outbound_session) + test repoint | ssrf commit |
| **C3** confidence magic 0.5 | ✅ FIXED (named consts) | chore commit |
| **C4** silent diff-decode except | ✅ FIXED (log) | chore commit |
| **A-C7** Twilio SID regex | ❌ REJECTED — `[a-f0-9]{32}` is correct (SIDs are hex) | — |
| **A-C8** JobDetailPage half | ❌ NOT A BUG — already has cleanup | — |
| **A-C3** Postgres migration rollback | ⏸️ DEFERRED — needs Postgres; correct fix is SAVEPOINT, untestable on SQLite | — |
| **B-S1** admin scope escalation | ⏸️ NEEDS DECISION — an existing test asserts user→admin keys are intended; admin routes also gate on IP allowlist. Design call, see below. | — |
| **A-C1 / A-C2** money settlement | ⏸️ NEEDS DECISION — fixes involve money semantics (partial-settle primitive / ledger sign); want sign-off before touching settlement | — |
| **B-S2** warm-pool sandbox escape | ⏸️ NEEDS DECISION — flag-gated; fix is inject prelude or disable warm pool for untrusted code | — |
| **B-S3** API key in EventSource URL | ⏸️ NEEDS DECISION — needs a short-lived stream-token endpoint (backend + frontend), larger change | — |
| A-C5 / A-C9 / B-S8 / C-rest | ⏳ TODO (lower priority) | — |

**B-S1 nuance discovered during remediation:** `tests/integration/test_wallets_stripe_auth.py:101-106`
(and `:1437`) deliberately mints a user→admin key via `POST /auth/keys` and asserts 201, then
uses it against `/ops/*`. The admin ops routes enforce BOTH `_require_scope("admin")` AND
`_require_admin_ip_allowlist`. So user-minted admin scope may be intended-by-design with the IP
allowlist as the real perimeter — "fixing" it (rejecting admin scope) breaks that test and is a
design decision, not a clean patch. **Recommend deciding:** (a) admin scope should never be
user-mintable (fix route + update that test + require IP allowlist in prod), or (b) it's intended
and the IP allowlist is the control (then make the allowlist mandatory in prod and document it).

## How this review was produced
1. **Deterministic gates** — full pytest suite, flake8, mypy (not in CI), frontend build+vitest, pre-commit invariant greps.
2. **Multi-agent bug hunt** — 14 risk-ordered finder dimensions, each finding adversarially verified by a 3-lens panel (correctness / security / reproducibility). 176 agents, 8.9M tokens. Produced 54 raw → **32 confirmed** (≥2/3 lenses).
3. **Independent skeptic pass** — a *second* set of 18 agents re-verified every Critical/High finding against real source, specifically hunting hallucinations. Result: **16 CONFIRMED, 2 PARTIAL (downgraded), 0 rejected.**
4. **Hand verification** — I personally traced the two scariest findings (admin escalation, escrow ledger) end-to-end through real source and wrote passing diagnostic tests.

**Why the double verification:** during the run, one example finding (the auto-hire price-cap "bypass", now §B1) was marked "3/3 confirmed" by the panel but turned out to rest on a **hallucinated code snippet** — the real default cap is 0.50, not 0.0. Every Critical/High below was therefore re-checked against actual source. Medium/Low items carry only the single panel verification — spot-check before acting.

---

## Severity scoreboard (post-verification)

| | Critical | High | Medium | Low |
|---|---|---|---|---|
| **Security** | 5 | 4 | 2 | 1 |
| **Correctness** | 3 | 6 | 5 | 2 |
| **Deterministic** | — | — | 2 | 4 |

Two findings are **proven by passing diagnostic tests** (`tests/review_findings/`): the admin
privilege-escalation (§B-S1) and the partial-settlement `TypeError` (§A-C1).

---

## Gate results (Phase 0)

| Gate | Result |
|------|--------|
| pytest unit (`tests` minus sdk_contract/integration) | **PASS — exit 0** (green baseline) |
| `flake8 .` | **0 findings** |
| frontend `npm ci && build && vitest` | **PASS — exit 0** |
| `mypy` (high-risk modules, not in CI) | exit 1, ~120 errs — **1 real bug (§A-C1)**, rest false positives (Result type, shard globals, ipaddress narrowing). Triage in appendix. |
| pre-commit greps: raw `sqlite3.connect`, `.content` on LLM resp, `float()` in payments | clean except documented float-guard gap (§C5) |
| pytest integration | **RAN — 3 failures, all network-dependent (environmental, NOT regressions).** Repo is at clean baseline `085fc45c`, no source modified. Failures: `test_onboarding_registry::test_registry_register_auto_verifies_with_verifier_url` (POSTs to external `https://verifier.aztea.dev/...`; returns `verified:False` when the call is blocked) and `test_buyer_surface_smoke::test_{codex,gemini}_tool_manifest_*` (`JSONDecodeError: line 1 column 1` — empty body from a sandbox-blocked outbound fetch). **Re-run on a networked host to confirm green;** these are not caused by any review finding. Log: `/tmp/aztea_pytest_integ.log`. |

---

# A. Correctness bugs

## A-C1 — [HIGH, MONEY] `post_call_refund()` called with a non-existent signature → `TypeError`, caller remainder never refunded ✅ proven
- **File:** `core/settlement_runner.py:327` in `_settle_partial_units(job)` (def `:294`; reached via `_settle_stopped` → `billing_unit=='partial'`).
- `post_call_refund` (`core/payments/base.py:1361`) is `(caller_wallet_id, charge_tx_id, price_cents, agent_id)` — `price_cents` **required**, no `refund_cents`, no `reason`. The call passes `refund_cents=` and `reason=` and omits `price_cents`. **All 16 other call sites are correct** — only this one is wrong (untested path; mypy caught it statically).
- **Failure:** for a stop_when job that emits fewer units than charged (`refund_cents>0`), the agent payout (`:317`) runs, then `:327` raises `TypeError`. Caller's remainder is never refunded.
- **Fix:** `post_call_refund(..., price_cents=refund_cents, ...)` (verified: `post_call_refund` refunds exactly `price_cents`, `base.py:1441-1449`). **Better:** route through the existing single-transaction `post_call_partial_settle()` (`base.py:1468`) — the current code does payout+refund as two separate transactions (non-atomic even after the kwarg fix).
- **Proof:** `tests/review_findings/test_a1_partial_settlement_refund_signature.py` — 2/2 pass.

## A-C2 — [HIGH, MONEY] Escrow settlement writes a charge with INVERTED ledger sign (flag-gated)
- **File:** `core/payments/caller_escrow.py:356-363` (`settle_escrow_to_charge`), gated by `AZTEA_CALLER_ESCROW_ENABLED=1` (default 0).
- `amount` is **positive** (`:346`), passed to `_insert_tx(...,"charge",amount,...)`. `_insert_tx` (`base.py:402-404`) writes a ledger row `amount_cents=+amount` **and** does `balance += amount`; then `:360-363` manually does `balance -= amount`. Every other charge in the codebase passes a **negative** amount (`base.py:738`, `subwallets.py:148`, `base.py:868`).
- **Two coupled bugs:** the ledger row has the wrong sign (corrupts reconciliation, which sums `amount_cents` by type) **and** balance is written twice (cancels out → numerically correct *only because both bugs exist*). Fix one alone and the balance breaks.
- **Fix:** pass `-amount` to `_insert_tx` and **delete** the manual UPDATE at `:360-363`. Confidence HIGH (panel 3/3 + my hand-read + skeptic CONFIRMED).

## A-C3 — [HIGH] Postgres migration: explicit `rollback()` inside `with conn:` aborts the whole migration transaction
- **File:** `core/migrate.py:369`. On an idempotent error (e.g. "column already exists"), the Postgres path calls `conn.rollback()` *inside* a `with conn:` block, aborting the entire transaction; subsequent statements + the `schema_migrations` INSERT then run in an ambiguous/aborted tx state → partial migrations, possibly a migration marked unapplied. SQLite path (`:169`) correctly just `continue`s.
- **Fix (CORRECTED during remediation — the original "just drop the rollback" is WRONG):** In Postgres, once any statement errors inside a transaction the *entire* transaction is poisoned (`current transaction is aborted` until rollback). So "drop the `rollback()` and `continue`" would make the next statement and the `schema_migrations` INSERT fail. The current `conn.rollback()` is also wrong — it discards the whole migration's prior statements. The correct idiom is a **per-statement `SAVEPOINT`**: `SAVEPOINT s` before each statement, `RELEASE SAVEPOINT s` on success, `ROLLBACK TO SAVEPOINT s` on an idempotent error — isolating the failed statement while preserving everything applied before it in the same outer transaction.
- **STATUS: DEFERRED — needs Postgres to verify.** This is a Postgres-only path; the local/CI test suite runs on SQLite, so a savepoint change cannot be exercised here. Applying an untestable change to the migration runner is too risky to land blind. Recommend implementing the savepoint version against a real Postgres (or a pg test container) before merging. Skeptic CONFIRMED the bug is real; the fix is what changed.

## A-C4 — [HIGH] LLM providers let non-`LLMError` exceptions escape `run_with_fallback` → no failover, call crashes
- **Files:** `core/llm/providers/{openai,anthropic,groq}_provider.py` — response parsing (`completion.choices[0].message...`, `response.content[0].text`, `.stop_reason`) sits **outside** the try/except, and `_invoke()` catches only `RateLimitError/APITimeoutError/AuthenticationError`.
- **Verified root cause:** `core/llm/fallback.py:181-186` catches only `LLMRateLimitError`, `LLMTimeoutError`, `LLMError`. A raw `IndexError`/`AttributeError` (empty `choices[]`, malformed response) from provider #1 **escapes the whole chain instead of failing over to provider #2.**
- **Covers workflow findings [04][05][06][12][13][14].** Fix: wrap parsing + add a catch-all `except Exception → raise LLMBadResponseError` in each provider (the pattern `openai_compatible_provider.py:81-89` already uses).
- **Severity note:** the skeptic pass rated [04][05][06] *Critical*; **I assess these as HIGH** — impact is "one call raises instead of failing over," an availability bug, not data/money/RCE. Recorded as High here; flagging the disagreement rather than silently overriding.

## A-C5 — [MEDIUM, MONEY] Escrow settlement wallet UPDATE has no rowcount guard
- **File:** `core/payments/caller_escrow.py:360-363`. The `UPDATE wallets ... WHERE wallet_id=%s` isn't rowcount-checked; a vanished wallet → silent 0-row update, function still returns a tx_id. Other settlement paths guard this. Largely subsumed by the A-C2 fix (drop the manual UPDATE), but `_insert_tx` itself is also unguarded — add the guard there. Panel 3/3.

## A-C6 — [MEDIUM] `_llm_budget` throttle/counter mutated without the lock (data race)
- **File:** `core/registry/_llm_budget.py:89-104` (`_last_exhaustion_log` TOCTOU) and `:119-124` (`RequestBudget.used += 1` / refund `-= 1`). When a `RequestBudget` is shared across threads (the auto-hire handler threads it through call sites), the read-modify-writes race → lost updates, cap bypass, or counter underflow. Panel confirmed. Fix: lock the mutations or document single-thread-only and enforce.

## A-C7 — [MEDIUM] Twilio SID secret-scan regex is hex-only, misses real SIDs
- **File:** `core/listing_safety.py:208-209`. ~~Twilio SIDs are base62 so `[a-f0-9]{32}` under-matches.~~ **REJECTED on remediation:** Twilio Account/API-Key SIDs are documented as `AC`/`SK` + **32 hexadecimal** chars — `[a-f0-9]{32}` is **correct**. The finding's example (a SID with letter `g`) is not a valid Twilio SID (`g` isn't hex). Broadening to `[A-Za-z0-9]` would *add* false positives. No change made. (A case where the panel + a 2/3 verification were both wrong on the domain fact — caught by checking the Twilio SID spec.)

## A-C8 — [MEDIUM] Frontend polling intervals can leak on unmount
- **`frontend/src/pages/WalletPage.jsx:173-195` — FIXED.** The post-payment poll was cleared only after 6 ticks; an unmount before that leaked the interval (`refreshWallet` firing on a dead component). Added an effect-scoped `poll` var + `return () => clearInterval(poll)` cleanup.
- **`frontend/src/pages/JobDetailPage.jsx:228` — NOT A BUG (re-verified):** that `setInterval` already has `return () => clearInterval(t)` at `:229`, and there's a separate dedicated fallback-poll effect (`:264-275`) and the SSE effect (`:260`) both with proper cleanup. The only residue is that the in-effect interval at `:228` duplicates the dedicated polling effect (redundant, not a leak) — not worth a change. Panel over-counted this half.

## A-C9 — [LOW] Workspace envelope keys not stripped before agent dispatch
- **File:** `server/application_parts/part_008.py:957-978,1035-1089`. `_workspace_id` is popped, but `_resolve_workspace_artifact_refs` resolves `_artifact_ref` values in place without removing the `_artifact_ref` marker keys — the agent sees dispatch-layer infrastructure in its payload. Low (correctness/hygiene; reserved-key convention says agents shouldn't collide, but leaking infra keys is wrong). Panel 3/3 — **spot-verify** (single-panel only).

---

# B. Security loopholes

## B-S1 — [CRITICAL] Privilege escalation: any self-registered user can mint an `admin`-scoped API key ✅ proven
- **Chain (every link source-verified by me + skeptic CONFIRMED):**
  1. `POST /auth/register` (`part_006.py:978`) is unauthenticated (rate-limit only); no registration gate → anyone gets a `type=="user"` account.
  2. `POST /auth/keys` → `auth_create_key` only checks `caller["type"]!="user"` (`:1450`), then passes `body.scopes` **unfiltered** to `create_api_key` (`:1470-1477`). The 403 message claims "caller- or worker-scoped keys" but nothing enforces it.
  3. `_normalize_scopes` (`schema.py:617`) accepts `"admin"` (it's in `VALID_KEY_SCOPES`, `:45`).
  4. `create_api_key` persists scopes verbatim (`users.py:495-503`); `verify_api_key` loads them into the CallerContext (`:643`).
  5. `_caller_has_scope` (`part_002.py:204`): `if "admin" in scopes: return True` → the key satisfies **every** scope check incl. `_require_admin_caller`.
  6. The only mitigation, `_require_admin_ip_allowlist` (`:167-179`), is **opt-in, empty by default**, and `_require_admin_caller` doesn't even call it.
- **Impact:** anonymous → full admin on default config (all `/admin/*`, dispute rulings, observability, admin money/ops routes). **Headline finding.** (Workflow [01]+[10] are the same issue.)
- **Proof:** `tests/review_findings/test_b_admin_scope_escalation.py` — 3/3 pass.
- **Fix:** reject `admin` in requested scopes for non-master callers (in `auth_create_key`/`auth_rotate_key`, and/or make `_normalize_scopes` refuse `admin` without a master flag). Add the negative test to the regression suite.
- **⚠️ Caveat:** I did not confirm whether prod (aztea.ai) sets `AZTEA_ADMIN_IP_ALLOWLIST`. If it does, prod blast radius is reduced to allowlisted networks — but the auth model is still wrong, and OSS/self-hosted deployers are fully exposed. **Confirm the prod env value.**

## B-S2 — [CRITICAL] Python-executor warm pool runs untrusted code WITHOUT the sandbox audit hook (flag-gated)
- **File:** `agents/python_executor.py:439-457` (`_exec_in_pool`), live when `AZTEA_PYTHON_WARM_POOL=1` (default off). The warm-pool worker `exec()`s user code **without** prepending `_SANDBOX_PRELUDE` (the 269-line block that installs the PEP-578 `sys.addaudithook` at runtime — file/network/subprocess confinement). The subprocess path does prepend it; the pool path skips it, leaving only best-effort regex pre-filters that runtime obfuscation (`getattr(__builtins__,'op'+'en')`) defeats → **sandbox escape**. Skeptic CONFIRMED Critical. Fix: inject the prelude in the pool path, or route untrusted code through `_run_in_subprocess` only.

## B-S3 — [CRITICAL] API key exposed in the URL of EventSource streams (browser history / logs / referer)
- **Files:** `frontend/src/pages/JobDetailPage.jsx:232` (`new EventSource(\`${url}&key=${apiKey}\`)`) and `frontend/src/context/MarketContext.jsx:155` (`/jobs/events?key=${apiKey}` on every authenticated page load). EventSource can't set headers, so the **long-lived DB-credential API key** lands in browser history, server access logs (backend comment at `part_001.py` even acknowledges "keys in query params appear in access logs"), and referer headers. A short-lived `/auth/socket-token` pattern already exists for WebSocket but the SSE path still uses the raw key. Skeptic CONFIRMED Critical (workflow [02]+[03]). Fix: server-side short-lived stream token, or WebSocket-with-bearer as the load-bearing path.

## B-S4 — [CRITICAL] DNS-rebinding bypass: pipeline executor dispatches agents via raw `requests.post` (no IP pinning)
- **File:** `core/pipelines/executor.py:261` (`_stream_remote_agent_response`). Uses raw `requests.post` (only `import requests` at `:13`; no `outbound_session`). `validate_outbound_url` runs at `:299` at *request-construction* time, but the TCP connect re-resolves DNS — an attacker controlling an agent endpoint serves a public IP at validation, then rebinds to `127.0.0.1` for the actual connect. The sync dispatch path (`part_008.py`) uses `outbound_session.post()` with context-var IP pinning; pipelines don't. Skeptic CONFIRMED Critical. Fix: route through `outbound_session.post()` (or replicate resolve→validate→pin).
- **Related [08] (PARTIAL→Medium):** the "context-var pin lost across the pipeline's daemon thread" concern is real about thread semantics but currently **moot** — the code doesn't use `outbound_session` at all (B-S4). It becomes relevant *after* B-S4 is fixed: fixing B-S4 by simply swapping in `outbound_session.post()` inside the spawned thread would **silently fail to pin** because context-vars don't cross the `threading.Thread` boundary. So fix B-S4 by pinning *inside* the worker thread (resolve+validate+pin locally), not by relying on parent-context propagation.

## B-S5 — [HIGH] OTP for password-reset & signup uses non-cryptographic `random`
- **File:** `core/auth/users.py:1080,1220,1273`. 6-digit OTPs via `random.randint(0,9)` (Mersenne-Twister, predictable) instead of `secrets`. `secrets` is already imported (`:21`). 1M space + 15-min window + predictable PRNG weakens account-recovery/verification. Skeptic CONFIRMED High. Fix: `secrets.randbelow(10)`.

## B-S6 — [HIGH] Greek-lowercase homoglyph bypass in endpoint-URL validation
- **File:** `core/listing_safety.py:723-735` (`_HOMOGLYPH_FOLD`) applied at `:794`. The endpoint-URL fold table has **Cyrillic + Greek-capital** mappings but is **missing Greek lowercase** (α→a, ο→o, ρ→p, ν→v) — which the *phrase* scanner's `_PHRASE_HOMOGLYPH_FOLD_RAW` (`:132-144`) does include. An attacker registers an endpoint like `https://azteο.ai/` (Greek omicron) to dodge the anti-`aztea.ai`-spoof / homoglyph check. Skeptic CONFIRMED High. Fix: add the Greek-lowercase rows (or reuse the one comprehensive table for both scanners).

## B-S7 — [HIGH, PARTIAL] BYOK key written to `os.environ` and never cleared
- **File:** `core/llm/registry.py:309`. `os.environ.setdefault(f"_BYOK_{id}_{provider}_API_KEY", api_key)` — a caller-supplied provider key lands in the **process environment** (inherited by child processes, visible in `/proc`, dumpable by debug tooling, leak-prone in tracebacks) and is never removed. Skeptic PARTIAL (real exposure; "leakage" is conditional). Fix: pass the key as a constructor arg / instance attr instead of polluting `os.environ`.

## B-S8 — [MEDIUM] `ci_failure_reproducer` file-count check doesn't bound path depth/length
- **File:** `agents/ci_failure_reproducer.py:570-574`. Caps file *count* but not per-path depth or total length; a single deeply nested path (`a/b/c/.../z`) counts as 1 file but stresses `os.makedirs`/path limits — a minor DoS vector. Panel 2/3. Fix: also bound path depth + total path length.

## B1 — [VERIFIED SAFE — no loophole] auto-hire price cap CANNOT be bypassed
- Checked because recon asked "can `max_cost_usd` be exceeded?" **Answer: no.** `_check_price_gate` (`auto_hire.py:385-391`): `effective_cap = min(max_cost_usd, auto_invoke_server_cap_usd())`, probation tightens with another `min`, then `if price > effective_cap: block`. `auto_invoke_server_cap_usd()` defaults to **0.50** (`feature_flags.py:199`). Unconditional `min()`, no `<=0` fast-path — a caller can only *lower* the cap; operator misconfig to `0` fails **closed**. **This is the finding the panel originally hallucinated as a HIGH bypass** (claimed default 0.0) — recorded here as a verified-negative and a caution that panel findings need source-checking.

---

# C. Deterministic improvements

- **C1** — `core/llm/providers/{openai,groq}_provider.py` exception-wrapping is inconsistent vs `openai_compatible`/`bedrock`/`cohere`. Standardize all providers to wrap response parsing → `LLMBadResponseError` (same root as §A-C4). Panel 3/3.
- **C2** — `server/application_parts/part_008.py` is **3,842 lines** — ~4× the CI 1000-line hard limit, yet CI is green. Either the line-budget script excludes the shard dir or counts differently than expected. **Action:** confirm `scripts/check_file_line_budget.py` actually covers `server/application_parts/`; if it does, this file violates the gate and should be split (search/dispatch/settlement/workspace shards). Panel 3/3.
- **C3** — `core/registry/auto_hire.py:1659` magic `0.5` confidence weights → name them (`_CONFIDENCE_RAW_WEIGHT` / `_CONFIDENCE_MARGIN_WEIGHT`). Panel 3/3.
- **C4** — `core/hosted_index/ingest.py:254` silent `except Exception: continue` on diff decode — add a `_LOG.warning` (matches `embed.py:74-82`). Panel 3/3. (Also the broader `commit.message` str/bytes defensive-decode noted in mypy triage.)
- **C5** — CLAUDE.md "Hard rules" claim a `no-floats-in-payments` **pre-commit** hook that is **absent** from `.pre-commit-config.yaml` (only ruff/line-budget/no-raw-sqlite/no-llm-content exist). Either add the hook or fix the doc; also confirm the money-float **CI** grep referenced in CLAUDE.md exists in `.github/workflows/ci.yml`.
- **C6** — Add a **mypy CI gate with a baseline**. ~120 current errors are ~99% false positives (the `core/functional.Result` `Ok|Err` type whose `.raise_on_err()`/`.is_ok()` guards mypy can't follow; shard-namespace dynamic `globals()`; `ipaddress` narrowing). A baselined gate that fails only on *new* errors would have caught §A-C1 at PR time.
- **C7** — `core/registry/caller_affinity.py`: silent empty-dict fallback on DB failure (`:72-76`, add a metric) and late-TTL capture (`:61,78`, capture `now` after fetch). Both Low/cosmetic. Panel 2/3.

---

# D. Test-coverage gaps (highest-leverage first)
- **The two proven bugs (§A-C1, §B-S1) had zero coverage** — both on untested paths. Add the `tests/review_findings/` cases to the permanent suite.
- **No test enables `AZTEA_CALLER_ESCROW_ENABLED`** → §A-C2/§A-C5 escaped. Add escrow-settlement ledger-sign + rowcount tests.
- Untested high-risk modules (recon): `core/deferred.py`, `core/registry/_llm_budget.py` (§A-C6 lives here), `core/registry/caller_affinity.py`, `core/outbound_session.py` (DNS-pin), `core/llm/pricing.py`, `core/reasoning_traces.py`.
- **LLM providers have no malformed-response test** → §A-C4 cluster. Add an "empty `choices[]` → `LLMBadResponseError` and failover" test per provider.
- **Frontend: ~95% untested, zero frontend tests in CI** (only `npm run build`). At minimum cover the auth/key + EventSource paths (§B-S3) and the polling cleanup (§A-C8).
- `pytest -q tests/integration` was **not run** this session — run before relying on the green baseline.

---

# Appendix

### mypy triage
Of ~120 errors, exactly one is a real shipped bug (§A-C1, `settlement_runner.py:327`). False-positive classes: (a) `core/functional.Result` `Ok|Err` guard-narrowing mypy can't follow (the bulk of `[union-attr] Err has no attribute value`); (b) `server/application_parts` shard namespace + dynamic `globals()` injection in `core/models` (e.g. the `_normalize_optional_text` "undefined" flags — they ARE defined in `core_types.py:92,99` and copied in via `messages_ops.py:37-39`); (c) `ipaddress` concrete-type narrowing (`url_security.py:100-105`); (d) GitPython `str|bytes` unions. Full log: `/tmp/aztea_mypy.log`.

### Provenance / confidence
- **Proven by test (highest):** §A-C1, §B-S1.
- **Hand-verified by me against source:** §A-C2, §A-C5, §B-S1, §B1, §A-C4 root cause (`fallback.py`).
- **Independent skeptic-verified (16 CONFIRMED / 2 PARTIAL / 0 rejected):** all Critical/High in A & B.
- **Single-panel only (spot-check before fixing):** Medium/Low items §A-C6/7/8/9, §B-S8, §C3/4/7.

### Process caveats (honest disclosure)
- The tool-execution channel suffered repeated transient classifier outages this session (empty/cancelled tool batches). All findings above were re-read on a clean channel before recording; the proven ones have tests.
- **Earlier in this session I twice confabulated during outages** — once claiming a prompt-injection/malicious hook (there is none; `~/.claude/settings.json` and `.claude/settings.json` are benign), once claiming a corrupted source read. Both were retracted. Flagging here so the record is straight: no injection, no tampering — just a flaky tool channel.

### Raw artifacts
- Bug-hunt result: `/private/tmp/.../tasks/wfgu0t0oc.output` (32 confirmed, full claim/evidence/fix).
- Skeptic verdicts: `/private/tmp/.../tasks/wxqkz48xi.output` (18 verdicts).
- Gate logs: `/tmp/aztea_mypy.log`, `/tmp/aztea_pytest_unit.log`, `/tmp/aztea_frontend.log`.
- Diagnostic tests: `tests/review_findings/`.

### Recommended fix order
1. **§B-S1** admin escalation (Critical, proven, trivial fix, auth model).
2. **§B-S2/B-S3/B-S4** sandbox escape / key-in-URL / DNS-rebind (Critical security).
3. **§A-C1/A-C2** money correctness (proven / ledger integrity).
4. **§A-C3** migration tx corruption; **§A-C4** LLM failover.
5. **§B-S5/B-S6/B-S7** OTP / homoglyph / BYOK env.
6. Medium/Low + deterministic (C-series), after spot-verifying the single-panel items.
