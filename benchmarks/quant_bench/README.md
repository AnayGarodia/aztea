# quant-bench — evaluation corpus for `quant_patch_validator`

This corpus is the validator's evaluation harness. It tells us — and any
prospective quant-firm buyer — what precision and recall the agent
delivers on **realistic AI-written patches to quant code**.

## Layout

```
entries/
  NNN_short_slug/
    pre.py                       buggy reference (the “before” code)
    gold.py                      the human-reviewed correct fix
    candidates/
      correct.py                 an AI-generated patch that's actually right
      subtle_regression.py       AI-generated patch with a real (subtle) bug
      broken_signature.py        AI-generated patch that breaks the contract
    labels.json                  { filename: "correct" | "regression" | "broken" }
                                 plus { "category": "...", "source": "...", "notes": "..." }
```

`pre.py` is **the differential reference** the agent runs each candidate
against. `gold.py` is documentation only — it shows what a sane fix looks
like and is not consumed by the validator. Some entries deliberately
have multiple `correct` candidates with stylistic differences (vectorized
vs loop, different intermediate variables) to make sure the validator
doesn't trivially equate "different syntax" with "different behaviour".

## How an entry is scored

The validator returns a `verdict` per candidate:

- `approved`  — agent declared the candidate behaviourally equivalent to `pre.py`
                (after intended-fix divergences are accounted for via `spec_hint`).
- `regression` — agent flagged a confirmed unintended divergence.
- `broken`    — agent detected a signature / contract mismatch.

We compute, across the whole corpus:

| Metric | Formula | v1 target |
|---|---|---|
| Precision | approved-and-actually-correct ÷ approved | ≥ 0.95 |
| Recall    | caught-regressions ÷ total-regressions | ≥ 0.80 |
| False-alarm rate | correct-flagged-as-regression ÷ correct | ≤ 0.05 |
| Broken detection | broken-correctly-flagged ÷ broken | ≥ 0.95 |

False-alarm rate is the killer metric — a quant team stops trusting the
agent the day it cries wolf about a clean refactor.

## How to add an entry

1. Pick a real bug (preferably a real OSS commit). Vendor `pre.py` and
   `gold.py` as **self-contained Python files** — no library imports
   beyond `numpy` / `pandas`. Strip dependencies you don't need.
2. Generate three or more candidates: at least one `correct` (a
   genuinely different correct implementation), at least one
   `subtle_regression` (your AI failure mode of choice), and at least
   one `broken_signature` (renamed function / re-ordered args / wrong
   return type).
3. Fill in `labels.json` — see existing entries for shape.
4. Run `python -m benchmarks.quant_bench.score` locally to confirm it
   plugs into the scorer.

## Failure-mode coverage

By the time we hit 30 entries, every category in the
[failure-mode taxonomy](../../docs/runbooks/quant-patch-validator.md)
must appear in at least one entry. v0.1's first 5 cover off-by-one,
unit-confusion, sign-flip, semantic-API-change, missing-factor.
