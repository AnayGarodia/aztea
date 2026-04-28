# 2026-04-28 Bug Hunt Handoff

This note captures the important context from the current bug-fixing pass so the repo is recoverable if the session gets rate-limited.

## What was fixed

### 1. `core/payout_curve.py`
- Fixed a real ledger bug in payout-curve clawbacks.
- The old code:
  - used direct wallet balance mutations
  - skipped rowcount checks
  - wrote unsupported transaction types (`payout_curve_clawback`, `payout_curve_refund`)
- The new code:
  - uses valid ledger semantics (`charge` and `refund`)
  - checks wallet existence / sufficient balance
  - fails closed without partial writes
  - keeps idempotency on `payout_curve:{job_id}`

### 2. Agent error-envelope cleanup
These agents now return structured top-level errors consistently:
- `agents/github_fetcher.py`
- `agents/hn_digest.py`
- `agents/arxiv_research.py`
- `agents/python_executor.py`
- `agents/image_generator.py`
- `agents/video_storyboard.py`
- `agents/web_researcher.py`

### 3. Graceful degradation when no LLM provider is configured
These agents used to crash after successful retrieval if synthesis/explanation failed due to no configured LLM provider:
- `agents/github_fetcher.py` (`summarize=true` now falls back to `summary=None`)
- `agents/hn_digest.py`
- `agents/arxiv_research.py`
- `agents/web_researcher.py`
- `agents/wiki.py`

They now return useful retrieval output instead of throwing.

### 4. `scripts/seed-demo.py`
- Fixed a real syntax error:
  - `global BASE` was declared after `BASE` had already been referenced inside `main()`
- Repo-wide `py_compile` now passes after this fix.

## Test coverage added / updated
- `tests/test_bug_regressions.py`
  - added payout-curve regression coverage
- `tests/test_agent_real_tool.py`
  - added coverage for structured error envelopes
  - added graceful no-LLM fallback coverage

## Validation run in this session
- `flake8` on all touched files: passed
- `py_compile` on all touched files: passed
- repo-wide `py_compile` over all `*.py`: passed
- import sweep over all `agents.*`: passed
- direct smoke checks for:
  - payout-curve clawback
  - HN fallback
  - arXiv fallback
  - GitHub fetcher fallback
  - web researcher fallback
  - wiki fallback
  all passed

## Important caveat
- Local `pytest` in this shell remains unreliable and often exits with code `-1` without a usable transcript.
- GitHub CI had been green before this pass, but the new tests from this pass have **not** been verified by a clean local pytest transcript in-session.

## Highest-value next steps
1. Run CI or a clean local pytest environment for:
   - `tests/test_agent_real_tool.py`
   - `tests/test_bug_regressions.py`
2. Continue the same bug-hunt pattern on remaining LLM-heavy internal agents:
   - `spec_writer`
   - `test_generator`
   - `pr_reviewer`
   - `package_finder`
   - `codereview`
3. Investigate the production reconciliation drift separately; this pass fixed a money-path bug, but it did not prove that the existing production drift came from payout curves.
