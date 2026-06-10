# Deference experiment report — OpenClaw + Hermes vs built-in tools

**Date:** 2026-06-10 · **Backend:** local Aztea server (branch `pratyushsinghal7/firecrawl-cost-experiment`, SQLite, port 8013) · **Corpus:** `corpus.json` (16 tasks × 4 categories, ground truth verified live at creation and frozen) · **Runs:** 64 (16 tasks × 2 modes × 2 harnesses), sequential, single rep.

## The question

Aztea's bet: an agent on a no-human-loop harness produces better/cheaper results by hiring an Aztea specialist for wedge tasks (web scrape, live lookup, sandboxed exec, dep audit) than by using its built-in tools. OpenClaw and Hermes are the test surfaces because their users have already removed the human from the loop.

## Methodology (and its limits)

- **Treatment arm ("aztea"):** Aztea MCP server registered + deference hook in **`block-all`** mode (every wedge category hard-blocks the native tool with a nudge to `auto_call_agent`). This is NOT the production default (warn-only for Bash, block for web). It was required because **warn-mode is invisible to the model in both harnesses** — OpenClaw surfaces warns only to its log, Hermes only honors blocks. That itself is a strategic finding: on these surfaces, advisory deference does not exist; only interception does.
- **Control arm ("builtin"):** same model, no MCP, no hook.
- **Models:** OpenClaw `anthropic/claude-sonnet-4-6`; Hermes `claude-sonnet-4-6` (isolated `HERMES_HOME`, API-key auth). Comparisons are within-harness.
- **Scoring:** mechanical string containment against frozen ground truth (`scorer.py`); no LLM judge. `infeasible` = run errored/timed out/empty.
- **Single rep per cell** — category-level aggregates (4 tasks × 4 runs) are the unit of analysis; per-task results are anecdotes.
- **Caveat:** the dep-audit and 2 of 4 live-lookup ground truths predate the model's training cutoff, so memory answers can pass them. Tasks `web-04` (unique echo marker) and `live-03` (example.com moved to Cloudflare NS after training data was written) are the genuine cannot-memorize discriminators; both arms passed both.

## Headline numbers

Correctness on scoreable runs (excluding `infeasible`):

| Arm | Pass | Fail | Correct rate |
|---|---|---|---|
| **builtin** | 29 | 0 | **100%** |
| **aztea (deference)** | 29 | 1 | **96.7%** |

Median wall-clock by category (seconds, OpenClaw / Hermes):

| Category | builtin | aztea | Faster arm |
|---|---|---|---|
| web_scrape | 9.1 / 11.0 | 87.5 / 22.7 | builtin (2–10×) |
| live_lookup | 17.3 / 35.2 | 19.9 / 20.5 | split (Hermes: aztea 1.7× faster) |
| sandboxed_exec | 8.3 / 13.7 | 27.9 / 28.8 | builtin (2–3×) |
| dep_audit | 39.8 / **105.1** | 66.3 / **22.3** | split (**Hermes: aztea 4.7× faster**) |

Cost: builtin spends $0 beyond LLM tokens. The aztea arm spent **51¢** in specialist fees (32¢ OpenClaw, 19¢ Hermes) **and ~2× the output tokens** (OpenClaw captured: 23,652 vs 12,082 output tokens) — deference *added* LLM cost rather than substituting for it, because blocked attempts and router retries lengthen the loop.

## The deference funnel (aztea arm, 16 runs per harness)

| Stage | OpenClaw | Hermes |
|---|---|---|
| Hook blocked a native wedge tool | 8/16 | 3/16 |
| Run deferred (≥1 Aztea job) | 13/16 | 7/16 |

Router outcomes across the experiment window: **12 invoked** vs **21 bounced** (`missing_fields` 12, `invocation_failed` 6, `schema_validation_failed` 2, `low_confidence` 1). Two-thirds of `auto_call_agent` attempts failed on the first try; models recovered via `search_agents` → `call_agent`, at the cost of the thrash documented below.

Hermes' low block count is itself informative: with a memorizable corpus, the model often answers without touching any wedge tool, so the hook never fires and nothing is bought.

## Findings

**F1 — The product loop was broken before any economics question applied (fixed during this work).** `call_agent`/`auto_call_agent` returned only `"Aztea job <id> | status: complete"` whenever the specialist's output lacked a `summary`-ish key. The Browser Agent fetched the full RFC; the buyer's model saw 80 characters of job status, gave up, and answered from memory. Fixed in `sdks/python-sdk/aztea/mcp/server.py` (`_job_output_text`, bounded JSON fallback, regression-tested). **Every paid call before this fix delivered zero value to the buyer.**

**F2 — The auto-hire router is the funnel's weak hop.** Same task, different phrasing: "scrape the web page at <url>" → Browser Agent at 0.612 confidence; "fetch the full text of <url>" → refused at 0.03. Cold-start made it worse: with zero completed jobs, every agent's `success_rate=0.0` suppressed ranking until the first few jobs settled. `missing_fields` (12 bounces) dominates: the router matches an agent but won't synthesize required payload fields.

**F3 — Router refusals cause expensive thrash.** Worst cells: `web-01/openclaw` 142s with 6 jobs and 6 blocks; `dep-04/openclaw` 239s with 12 jobs (vs 51s builtin). The model loops retry-rephrase-retry against blocks and refusals. This is where the 2× token overhead comes from — **a blocked tool with a bounced redirect is strictly worse than either letting the tool run or successfully redirecting.**

**F4 — The one deference-caused wrong answer is a trust failure, not a capability failure** (`exec-02/openclaw/aztea`). Auto-hire synthesized `{"code": "run this Python: import random; ..."}` — natural language inside the code field → SyntaxError. The buyer's model then reported a fabricated number (`654682271` vs true `686579304`) and attributed it to the specialist. Builtin ran the same task correctly in 8s. Chain to fix: payload synthesis quality, and result envelopes that make "the code did not run" unmistakable.

**F5 — Where deference genuinely won: capability gaps, not commodity tools.** `dep_audit` on Hermes: builtin medians of 81–105s with one 280s timeout (the model hand-rolls OSV probing in a terminal), vs 16–37s deferred with 100% pass. The Dependency Auditor specialist is *structurally* better than a model improvising the same audit. This is the thesis surviving in miniature — and note it won on **speed and reliability**, not correctness.

**F6 — Where deference is a pure tax: anything the harness's own tools do in one step.** Web fetch, DNS via `dig`, hash/stddev via local exec — builtin was equally correct, 2–10× faster, and free. Even the cannot-memorize tasks (`web-04`, `live-03`) were handled perfectly by builtin tools. Blocking these only added latency, tokens, and 3¢/call.

**F7 — `exec-03` excluded (symmetric artifact):** the Anthropic API hard-refuses the prompt's base64 blob (`stop_reason: refusal, category: bio` — classifier misfire on random-looking encoded content). All four cells failed identically at the model layer; the cell measures nothing about deference.

## Don't-route list (per this data)

- **Don't block ad-hoc exec** (hash, decode, arithmetic): local exec is correct, instant, free; the specialist round-trip only adds the F4 failure mode.
- **Don't block plain URL fetches** on harnesses with a working fetch tool.
- **Don't block live lookups the model can do with one command** (`dig`, registry JSON endpoints).
- **Do route**: multi-step audit work (dep audit on Hermes won 4.7× on speed and dodged a timeout), and any category where the harness lacks the tool entirely — that's a pull decision the model can make itself, not a push the hook must force.

## Thesis verdict

**As tested — push-based deference on commodity wedge tasks against a frontier model with working built-in tools — the thesis fails.** Deference was net-negative: equal-at-best correctness (96.7% vs 100%), slower in 12 of 16 category-cells, ~2× token cost, plus specialist fees, plus a new wrong-answer mode introduced by the routing layer itself.

**The narrower thesis survives:** specialists win where they carry *structural* capability the harness lacks (the Hermes dep-audit result), and the integration plumbing — attribution, billing, refunds, the deference log — all worked end-to-end on both real harnesses. But the mechanism should be **pull, not push**: expose Aztea's catalog as tools the model elects when it hits a capability wall, rather than blocking tools it already uses well. The block-all hook is the right *experiment instrument* and the wrong *product default*.

**Would an OpenClaw/Hermes builder keep this installed?** The MCP registration: plausibly yes — it cost nothing when unused and saved the Hermes dep-audit runs. The block-mode deference hook: no — it taxed 12 of 16 categories for the benefit of 1. Fix the router (F2/F3) and the payload synthesis (F4) before any push mechanism is defensible even where specialists win.

## Reproduce

```bash
# local server on :8013 (own DB + worker lock), funded caller, MCP installs:
#   see plan file; configs/ holds the per-mode OpenClaw configs (gitignored — carries the local key)
python3 experiments/deference/runner.py --dry-list   # cell matrix / resume state
python3 experiments/deference/runner.py              # run pending cells (sequential)
python3 experiments/deference/scorer.py --summary    # per-category rollup
.venv/bin/pytest -o addopts="" -q tests/test_deference_experiment.py
```

Raw rows: `results/runs.jsonl` (64 rows; every number in this report derives from them via `scorer.py`).
