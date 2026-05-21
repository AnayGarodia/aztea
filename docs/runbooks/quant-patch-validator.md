# Quant Patch Validator — operator runbook

> **What it is.** A built-in agent that finds bugs in AI-written quant
> code by differentially fuzzing two implementations (reference vs
> candidate) and triaging divergences. Designed for AI-suggested patches
> to numerical / trading-logic Python.
>
> **Why it exists.** Quant teams can't trust AI-written code to run in
> production. This agent gives them a high-precision verifier: every
> reported bug ships with a reproducer that runs in a sandbox.

## Quick reference

| Field | Value |
|---|---|
| Agent ID | `0552b418-026d-5609-8446-2fe7af0efa56` |
| Internal endpoint | `internal://quant_patch_validator` |
| Price (v1) | $1.50 (flat) |
| Default tier | `standard` (5-min fuzz) |
| Category | Code Quality |
| `examples_sensitive` | **True** — caller code is never replayed |
| Source | `agents/quant_patch_validator/` |
| Bench corpus | `benchmarks/quant_bench/` (≥30 entries) |
| Bench score command | `python -m benchmarks.quant_bench.score` |

## How callers invoke it

### Direct REST

```bash
curl -X POST http://localhost:8000/registry/agents/0552b418-026d-5609-8446-2fe7af0efa56/call \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d @- <<EOF
{
  "reference_code": "def f(x): return x*2\n",
  "candidate_code": "def f(x): return x+x\n",
  "fuzz_budget": "quick",
  "fuzz_seconds": 6
}
EOF
```

### MCP (`call_specialist`)

```python
call_specialist(
    agent="quant_patch_validator",
    input={
        "reference_code": "...",
        "candidate_code": "...",
        "fuzz_budget": "standard",
    },
)
```

### Why `do_specialist_task` won't auto-hire this agent

The auto-hire price gate caps at $0.10. Even the lowest tier of this
agent is $1.50 (flat). So the agent is **explicit-only**: the caller
must consciously hire it. This is the right UX for "I'm sending you
proprietary alpha for validation" — every call is a deliberate act.

## How to read the output

### Top-level verdict

| `verdict` | Meaning | Action |
|---|---|---|
| `equivalent` | No divergences found within budget | Approved; merge with confidence |
| `regressions_found` | One or more value-divergence clusters | Block the patch; review reproducers |
| `contract_broken` | Return type / shape / always-raises mismatch | Block; the patch breaks all callers |
| `signature_divergence` | Function name / arity changed | Block; this is not the same function |
| `intended_changes_only` | Divergences confirmed to match `spec_hint` | Approved; the patch is doing what it claims |

### When `verdict == regressions_found`

Look at `confirmed_regressions[]`. Each cluster has:

- `cluster_id` — internal handle
- `member_count` — how many inputs hit this cluster (more = more confident)
- `verdict` — `regression` | `both_wrong` (see below)
- `hypothesis` — root-cause guess from triage (LLM or heuristic)
- `confidence` — 0..1; useful for prioritising review
- `representative.inputs` — the minimal failing input (shrunk)
- `representative.divergence_detail` — exact divergence (max abs diff,
  exception class, shape mismatch, etc.)

### When `verdict == both_wrong`

The reference AND the candidate disagree with each other in a way that
suggests neither is consistent with the stated spec (e.g. both raise
different exceptions on the same edge case). **Escalate to a human:**
this often surfaces a latent bug in the *reference* code that the team
didn't know existed. Don't auto-block the patch — investigate.

## When the agent is wrong

### False positive (flagged a clean refactor as a regression)

Three usual causes:

1. **Reduction-order drift.** Cumsum vs loop sum, BLAS dot vs elementwise
   — these produce bit-level differences at the noise floor. The agent's
   default `rtol=1e-5 atol=1e-7` is calibrated to tolerate this for
   typical quant inputs (max |input| ≤ 1e6, array sizes ≤ 250). If the
   caller's inputs are larger, **bump rtol** in the call payload.

2. **Statelessness violated.** If the function depends on hidden global
   state (a module-level random seed, a cached lookup table) the
   differential harness will surface ghost-divergences. **Move the state
   to an explicit parameter** in both implementations, then re-fuzz.

3. **`auto_tune_tolerance` enabled on a time-ordered function.** The
   permutation-based tolerance estimator silently over-tolerates when
   the function is order-dependent (rolling stats, RSI, EWMA). **Set
   `auto_tune_tolerance: false`** for these.

### False negative (missed a real bug)

1. **Untyped parameters.** The name-based heuristic in `signature.py`
   catches the common cases (`prices`→ndarray, `window`→int), but if
   the parameter has an unusual name, the fuzzer falls back to floats.
   **Add type hints** to both implementations and re-run.

2. **Budget too small.** Bugs that only fire on specific shapes (e.g.
   single-element arrays, all-zero inputs) may not be reached in
   `quick` tier. **Re-run with `fuzz_budget: standard`** for any
   patch where `inputs_explored < 5000`.

3. **Bug shows up only on inputs outside the default range.** The
   default float range is `[-1e6, 1e6]`. If the function's failure mode
   requires inputs > 1e10 (rare but possible in fixed-income / yield
   calculations), the fuzzer won't reach it. There's no caller-tunable
   knob for this in v1 — file an issue.

## Workspace audit trail

When called with `_workspace_id`, the agent writes
`qpv/report.json` (and, on signature divergence,
`qpv/signature_divergence.json`) to the workspace. On successful job
completion the workspace is sealed — the cryptographically-signed
manifest is the audit record for the validation run. This is the
v0.1 answer to "we need to prove to compliance that we validated this
patch."

## Operator escalation

| Symptom | Probable cause | Fix |
|---|---|---|
| Every patch returns `equivalent` | Caller likely passed `fuzz_seconds=0` or wrong code | Check input payload |
| `contract_broken` for a known-equivalent patch | Return type is `pd.Series` vs `np.ndarray` | Cast both sides; this is a real contract change |
| LLM triage classifications look wild | LLM key misconfigured, hitting fallback heuristic | Check `triaged_by` field; if `heuristic` on everything, fix LLM provider chain |
| Bench scoring fails CI | Re-run with `QUANT_BENCH_FUZZ_BUDGET=standard` and inspect per-entry breakdown | Likely a new corpus entry has a real bug in `pre.py` |

## On-prem / data sensitivity story

The agent processes proprietary alpha. v1's protections:

1. `examples_sensitive: True` blocks the work-example recorder from
   replaying caller code.
2. Workspace artifacts (which include the candidate code) are written
   only when the caller explicitly opts in via `_workspace_id`.
3. The agent makes no outbound HTTP call beyond the configured LLM
   provider (optional — triage falls back to a deterministic heuristic
   when no LLM is configured).

For customers with stricter requirements, the agent is fully
self-contained: setting `AZTEA_LLM_DEFAULT_CHAIN=""` disables the LLM
triage step and the agent runs purely deterministically. This is the
v0.1 answer to "we don't want anyone seeing the code."

A formal on-prem package is on the v0.2 roadmap.

## Bench corpus

| Path | Purpose |
|---|---|
| `benchmarks/quant_bench/entries/NNN_slug/pre.py` | Reference (buggy or canonical) |
| `benchmarks/quant_bench/entries/NNN_slug/gold.py` | Documented correct fix |
| `benchmarks/quant_bench/entries/NNN_slug/candidates/` | AI-generated patches with labels |
| `benchmarks/quant_bench/entries/NNN_slug/labels.json` | Ground truth |
| `benchmarks/quant_bench/score.py` | Aggregator: precision / recall / false-alarm |

To add a new entry, see `benchmarks/quant_bench/README.md`. The
selection rule is one entry per failure-mode category until all 9
are covered (off_by_one, sign_flip, unit_confusion, missing_factor,
semantic_api_change, NaN propagation, time-axis, lookahead-bias,
contract-shape).

## CI gates

`tests/integration/test_quant_patch_validator_corpus.py` enforces:

- Precision ≥ 0.95
- Recall ≥ 0.80
- False-alarm rate ≤ 0.05
- Broken-detection rate ≥ 0.95

These thresholds match those documented in
`benchmarks/quant_bench/README.md`. A change in one requires a change
in the other.

The corpus test is marked `slow` (~10 min wall-clock at quick tier);
CI runs it on `pytest -m slow` only. Operators should run it locally
after any change to the agent's fuzz / triage / harness logic.

## Test suite inventory

The validator's test surface lives across these files. Run them in
separate `pytest` invocations — combining the integration + lifecycle
tests in one process can hit a coverage/SQLite/threading interaction
that segfaults on macOS (documented v1 flake).

| File | Marker | What it covers |
|---|---|---|
| `tests/test_quant_patch_validator_unit.py` | none | Per-module unit tests (signature, harness, cluster, triage, report, atheris, coverage) — 34 tests, < 5 s |
| `tests/test_quant_patch_validator_oracle.py` | none | Diff-oracle correctness matrix (scalars / arrays / containers / pandas / exceptions) — 42 parametrised cases |
| `tests/test_quant_patch_validator_signature.py` | none | LLM enrichment (mocked), name heuristic, signature-pair compatibility — 14 tests |
| `tests/property/test_quant_patch_validator_invariants.py` | `property` | Self-equivalence, schema closure, no-raw-exceptions-escape — Hypothesis-driven |
| `tests/integration/test_quant_patch_validator_edge_inputs.py` | none | All input-validation error envelopes (27 tests) |
| `tests/integration/test_quant_patch_validator_security.py` | `security` | Hostile candidates: infinite loops (subprocess SIGKILL), recursion, self-import, FS / sys.path containment surface (relative paths + sys.path mutations contained; absolute-path FS still escapes — v0.2 plan) |
| `tests/integration/test_quant_patch_validator_workspace.py` | none | `_workspace_id` artifact writes, best-effort failure (6 tests) |
| `tests/integration/test_quant_patch_validator_concurrency.py` | none | Parallel-call safety, tempfile cleanup, sys.modules pollution (5 tests) |
| `tests/integration/test_quant_patch_validator_lifecycle.py` | none | HTTP end-to-end via TestClient (4 tests) |
| `tests/integration/test_quant_patch_validator_async.py` | `slow` | Deep-tier async via POST /jobs (2 tests) |
| `tests/integration/test_quant_patch_validator_scenarios.py` | `slow` | Quant-firm flows: pre-merge CI, BLAS-vs-elementwise, hot-path opt, numpy alias, full bench run (9 scenarios) |
| `tests/integration/test_quant_patch_validator_corpus.py` | `slow` | Bench precision / recall / false-alarm / broken-detection gates |
| `tests/contract/test_quant_patch_validator_contract.py` | none | Spec ↔ agent ↔ runbook drift detection (11 tests) |

**~170 tests total.** Run profiles:

```bash
# Fast suite (~95s on M-class macOS)
pytest tests/test_quant_patch_validator_unit.py tests/test_quant_patch_validator_oracle.py tests/test_quant_patch_validator_signature.py tests/contract/test_quant_patch_validator_contract.py tests/integration/test_quant_patch_validator_edge_inputs.py -p no:cacheprovider

# Security + property + workspace + concurrency (~95s)
pytest tests/integration/test_quant_patch_validator_security.py tests/integration/test_quant_patch_validator_workspace.py tests/integration/test_quant_patch_validator_concurrency.py tests/property/test_quant_patch_validator_invariants.py -p no:cacheprovider

# Lifecycle (~14s; run alone to avoid the macOS segfault)
pytest tests/integration/test_quant_patch_validator_lifecycle.py -p no:cacheprovider

# Slow / scenarios / corpus (~10–15 min; nightly)
pytest -m slow tests/integration/test_quant_patch_validator_async.py tests/integration/test_quant_patch_validator_scenarios.py tests/integration/test_quant_patch_validator_corpus.py
```

## v1 containment surface (2026-05-20 — post-isolation hardening)

The candidate is loaded inside an `IsolatedWorker` subprocess
(`agents/quant_patch_validator/isolation.py`) with its own per-Harness
tempdir as cwd. Per-call timeout is enforced by terminating the
subprocess — SIGTERM first, SIGKILL after a 0.3 s grace window. The
reference is trusted and still runs in-process under a daemon-thread
wall-clock cap.

**What v1 contains:**

- **Per-call timeout: 2.5 s.** Subprocess SIGKILL on timeout works for
  pure-Python AND C-extension infinite loops. The worker is respawned
  on the next call. Tested by `test_infinite_loop_candidate_caught_via_per_call_timeout`.
- **Relative-path filesystem writes.** Land in the worker's tempdir
  (per-Harness, deleted on close). Tested by
  `test_relative_path_write_is_isolated`.
- **sys.path mutations.** Confined to the subprocess; the parent's
  sys.path is unchanged. Tested by `test_sys_path_modification_is_isolated`.
- **Module-state mutations.** Don't bleed into the parent's process
  state. The reference module is fresh on every Harness construction.
- **Signal-handler hijacks.** Worker resets to SIG_DFL on startup so a
  candidate cannot intercept the parent's SIGTERM.

**What still escapes (v0.2 closes via `live_sandbox`):**

- **Absolute-path filesystem writes** — cwd-based containment only
  blocks relative paths; a candidate opening `/tmp/x` or `~/.ssh/x`
  still writes there. Documented by `test_absolute_path_write_still_escapes_v1`.
- **Network egress.** No seccomp / namespace filter in v1; a candidate
  can `requests.get(...)` freely.
- **Resource exhaustion** (memory bomb, fork bomb). No cgroup limit.
- **Dynamic `__import__('agents.' + 'quant_patch_validator')`** still
  bypasses the static self-import block (the worker has the same
  module surface area). Tested by `test_self_import_via_string_construction_not_blocked`.

**Operational caveats:**

- **`track_coverage=True` returns `coverage_pct: null` in v1.**
  Coverage.py instruments via `sys.settrace` in-process and does not
  span the subprocess boundary. The agent logs an informational
  message and runs the fuzz without coverage data. v0.2 plan: invoke
  coverage.py inside the worker and pipe results back via the same
  multiprocessing pipe.
- **Per-Harness subprocess startup cost** (~50–150 ms on Linux fork,
  slower on macOS/Windows spawn). The worker is long-lived once the
  Harness is constructed, so the cost amortises across thousands of
  fuzz iterations.
- **`auto_tune_tolerance=True` over-tolerates time-ordered functions**
  (RSI, rolling-window). Leave it off for stateful code; documented in
  `test_scenario_e_autotune_overtolerates_stateful_function`.
