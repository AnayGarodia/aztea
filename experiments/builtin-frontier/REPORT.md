# Built-in capability frontier: where OpenClaw + Hermes struggle without a specialist

**Date:** 2026-06-10 · **Harnesses:** OpenClaw (2026.5.27) + Hermes, both `claude-sonnet-4-6`, **built-in tools only, no Aztea** · **Corpus:** 72 tasks × 12 categories (`corpus.json`, ground truth frozen at creation) · **Runs:** 144 (72 × 2 harnesses), single rep, sequential.

## The question

Before Aztea pushes any specialist, we need the honest demand map: with their own built-in tools, where do real agent harnesses actually fail? Every task where **both** OpenClaw and Hermes fail or fake it is a spot a specialist could earn its place. This experiment touches no Aztea code — it's the clean built-in baseline.

## Headline result

**Overall built-in success: 124/144 (86%).** The agents are far more capable than the "they'll need specialists everywhere" prior. **10 of 12 categories are at or near 100% for both harnesses** — including several I expected to be hard. The genuine frontier is narrow and specific.

| Category | OpenClaw | Hermes | Verdict |
|---|---|---|---|
| web_stable | 6/6 | 6/6 | both fine |
| web_dynamic | 6/6 | 6/6 | both fine (they find the JSON/AJAX endpoint or render it) |
| document_extraction (PDF) | 6/6 | 6/6 | both fine (OpenClaw has a `pdf` tool; Hermes shells out) |
| ocr_scanned | 6/6 | 5/6 | both fine (vision reads the images; Hermes off-by-2 once) |
| live_lookups (CVE/DNS/cert/version) | 6/6 | 6/6 | both fine (curl + real data) |
| numeric_compute | 6/6 | 6/6 | both fine (they actually run code) |
| cross_language (Rust/Go/numpy/C) | 6/6 | 6/6 | both fine — **they bootstrap the toolchain** |
| static_security | 5/6 | 6/6 | both fine (reasoning + osv/curl) |
| infra_config (k8s/tf/openapi/docker) | 6/6 | 6/6 | both fine (LLM reasoning suffices at this scale) |
| browser_automation | 6/6 | 6/6 | both fine (bundled Chromium nav works) |
| **accessibility_perf** | 3/6 | 3/6 | **partial frontier** |
| **web_walled** | 0/6 | 0/6 | **hard frontier** |

Median wall time: OpenClaw 14s, Hermes 13s.

## The demand map: 8 tasks where BOTH harnesses struggle

This is the deliverable — reproducible from `runs.jsonl` via `scorer.py --struggle`. Two clusters:

### Cluster 1 — Walled content (6/6, all `expected_hard`): the structural wall

| Task | What it asked | OpenClaw | Hermes | Specialist that fixes it |
|---|---|---|---|---|
| ww-01 | Summarize an authenticated LinkedIn feed | refused | refused | Authenticated-session / credentialed-fetch agent |
| ww-02 | Quote a paywalled WSJ article | refused | refused | Licensed-content / paywall-access agent |
| ww-03 | Call OpenWeatherMap (needs key) | refused | refused | Keyed-API gateway (holds the credential) |
| ww-04 | Real-time AAPL bid/ask | refused | refused | Paid market-data feed agent |
| ww-05 | Google #1 result for a phrase | refused | partial | Search/SERP API agent (past the bot wall) |
| ww-06 | Latest Instagram caption (NASA) | **hallucinated** | refused | Authenticated social-fetch agent |

**This is the clean specialist demand.** Built-in tools cannot cross an auth/paywall/key/CAPTCHA boundary — and crucially, the failure is mostly *honest refusal* (the right behavior). The exception is the dangerous one: **ww-06, where OpenClaw invented a confident, detailed NASA Instagram caption — fabricated post shortcode, fabricated Artemis III crew — for content it could not access.** A credentialed-fetch specialist replaces both the refusals and the fabrication with a real answer. This is exactly where a specialist that *holds the credential* is worth paying for.

### Cluster 2 — Real measurement you can't trust (2 tasks): the verification wall

| Task | What it asked | OpenClaw | Hermes | Why it's a struggle |
|---|---|---|---|---|
| ap-02 | Lighthouse performance score of example.com | hallucinated | hallucinated | Both confidently returned "100/100" — the trivially-guessable answer for a static page — with no evidence of an actual Lighthouse run |
| ap-03 | LCP of wikipedia.org in ms | partial | hallucinated | OpenClaw showed a real Puppeteer measurement (1072ms); Hermes reported a *different* value (572ms) with no tool evidence. You cannot tell a measurement from a guess. |

**The frontier here isn't "can't do it" — it's "you can't trust the number."** The agents will emit a plausible performance/audit figure whether or not they really measured. A **Lighthouse/Web-Vitals specialist that guarantees a real, reproducible measurement** is the value — not raw capability, but *verifiable* capability.

## Asymmetric & cautionary cases (one struggled, one didn't)

These don't make the both-struggle list but inform the roadmap:

- **ap-01 (axe accessibility):** OpenClaw ran a real axe-core audit (`exec`+`write`, correct 2 violations) → success; Hermes gave the same correct result with no tool evidence → only `partial`. Accessibility auditing *is* achievable built-in — but reliably only when the harness actually installs and runs the tool.
- **ap-04 (imgs missing alt):** Hermes correct (1); **OpenClaw wrong (2) — it invented a "Facebook tracking pixel"** not on the page. Confident DOM hallucination on an otherwise-easy task.
- **oc-03 (chart OCR):** OpenClaw read the bar value exactly (1473); Hermes was off-by-two (1475). Vision precision varies — fine for gist, risky for exact figures.
- **ss-06 (lodash CVE):** Hermes named the right CVE via `osv_check`; OpenClaw cited "Snyk DB" and missed the specific CVE id. Live-vuln-data reliability favors the harness with a real audit tool.

## Surprises (tasks I tagged `expected_hard` that the agents solved)

The prior said PDF, OCR, and compiled languages would be struggles. They weren't:

- **PDF extraction (6/6 both):** OpenClaw has a built-in `pdf` tool; Hermes shells out. The "no PDF parser" gap doesn't exist in practice.
- **OCR of scanned images (11/12):** vision models read the rendered receipt/chart images directly — no `tesseract` needed. (`tesseract` is *absent* on this machine; they didn't need it.)
- **Cross-language (12/12):** the agents **bootstrap missing toolchains** — installing Rust/Go on the fly (cl-01 took OpenClaw 80s, cl-02 114s) to compile and run. `cargo`/`go` were absent at start; the agents fetched them.

The lesson: a frontier model with shell access and a browser **routes around** most "missing tool" gaps by installing or improvising. Specialist demand is NOT "the harness lacks tool X" — it's "the boundary is structural (auth/credential/paid-data) or the answer needs verification the agent can't self-provide."

## Don't-build list (both fine — a specialist would be a tax)

Web fetch/scrape (static and dynamic), PDF/document extraction, image OCR/vision, live CVE/DNS/cert/version lookups, code execution and numeric compute, cross-language builds, small-scale SAST/secret-scan/dep-audit, infra-config validation, and basic browser automation. The deference experiment's "don't-route commodity work" conclusion holds from the other direction: these are commodity for a modern harness.

## Honest caveats

- **Single rep** — category-level aggregates (6 tasks/harness) are the unit of analysis; per-task results are anecdotes. The audit/perf adjudications are judgment calls (documented with rationale in `adjudications.json`); a stricter reviewer might score ap-01/ap-03 differently, which would only *widen* cluster 2.
- **Hermes web backends unconfigured** on this machine (no Firecrawl/Tavily/Exa keys) — it reached the web via `terminal` curl and bundled browser. It still hit 6/6 on web_stable and web_dynamic, so the confound didn't change the verdict, but Hermes's per-run tool capture is best-effort (one-shot prints only final text).
- **Version-drift tasks** (npm/PyPI latest) frozen at 2026-06-10.
- **The fabrication finding (ww-06, ap-02, ap-04) is the most important and the least about capability:** the risk isn't that agents can't do these — it's that they confidently *pretend* to, which is precisely the failure a verified specialist removes.

## Reproduce

```bash
.venv/bin/python experiments/builtin-frontier/fixtures/make_fixtures.py   # regen fixtures
python experiments/builtin-frontier/runner.py            # builtin-only, no Aztea, resumable
python experiments/builtin-frontier/scorer.py --summary  # per-category
python experiments/builtin-frontier/scorer.py --struggle # the demand map
.venv/bin/pytest -o addopts="" -q tests/test_builtin_frontier.py
grep -ri aztea experiments/builtin-frontier/results      # empty — no Aztea touched the baseline
```

Raw rows: `results/runs.jsonl` (144). Every number here derives from them via `scorer.py` + `adjudications.json`.
