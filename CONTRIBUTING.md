# Contributing to Aztea

Thanks for considering a contribution. Aztea is **Apache-2.0** licensed; by submitting a pull request you agree your contributions are licensed under those terms and certify the [Developer Certificate of Origin](https://developercertificate.org/) (sign your commits with `git commit -s`).

This file is the short version. The deep engineering-style and architecture reference is [`CLAUDE.md`](CLAUDE.md) — read it before changing money flows, auth, migrations, or the MCP surface.

---

## Before opening a PR

1. **Read `CLAUDE.md`'s "Engineering style" section.** It is not aspirational — CI enforces the hard rules and reviewers enforce the soft ones.
2. **Search the codebase before adding a utility.** `fmtDate` lives in 10+ files because someone skipped this once; we don't want to repeat that.
3. **One concern per PR.** Bug fixes don't need surrounding cleanup; refactors don't need bundled features.
4. **Trace every caller in the same change.** If you alter a function signature, grep and update every caller in the same diff. Partial refactors are worse than no refactor.

---

## Hard rules (CI enforces — do not bypass)

- **File length:** hard limit 1000 lines (`scripts/check_file_line_budget.py`). Soft warn at 500 for new files. Split before extending past 500.
- **Function length:** ~80 statements max, cyclomatic complexity ≤ 10.
- **Catch blocks:** never empty, never bare `except:`. Either log structured context with the exception or re-raise.
- **No silent fallbacks.** Every except path either logs or re-raises.
- **Magic numbers:** name them. Money, ratio, timeout, and limit constants live as module-level `UPPER_SNAKE`. Allowlist: `0`, `1`, `-1`, `2`, simple HTTP status codes.
- **Money paths:** never `float()` in `core/payments/` or settlement code. Integer cents only. CI greps for floats in money modules.

---

## Soft rules (reviewers will flag)

- **Re-read after writing.** Match the file's existing style and patterns, not your defaults.
- **Boy scout rule.** When you touch old code, leave it slightly better.
- **Comment WHY, never WHAT.** Don't reference issue numbers or task names — that's what the PR description is for.
- **Prefer pure functions.** Push side effects to the edges (HTTP routes, DB writes, filesystem).
- **One-way dependencies.** Business logic in `core/` must not import from `server/routes/`, `frontend/`, or transport layers.
- **Make illegal states unrepresentable.** Use enums + discriminated unions over scattered runtime checks.

---

## Critical invariants

These will be rejected in review even if they "pass":

- **Integer cents only** in money paths.
- **Insert-only ledger.** `transactions` only ever gets INSERT. Corrections are compensating entries.
- **Single connection manager.** All modules use `core/db.py`. Never open a raw `sqlite3.connect()`.
- **Migrations are idempotent and never deleted.** Add new ones with the next sequence number.
- **`caller_ratings` lives only in `core/reputation.py`.** Don't re-declare or migrate this table elsewhere.
- **All outbound URLs go through `core/url_security.py`.** SSRF protection is non-negotiable.
- **Never hardcode `aztea.ai`.** All hosted-service calls go through `core/hosted_client.py`. The OSS build must run fully self-contained when `AZTEA_HOSTED_API_URL` is unset.

Full list in [`CLAUDE.md` → "Critical invariants"](CLAUDE.md#critical-invariants--never-violate-these).

---

## Workflow

```bash
# Fork, clone, branch
git checkout -b feat/your-feature

# Develop
pip install -r requirements.txt
pytest tests --ignore=tests/test_sdk_contract.py -q     # full suite passes
python scripts/check_file_line_budget.py                # green

# Commit (DCO required)
git commit -s -m "concise: short description"

# Push and open PR
```

Use the PR template. CI will run the full suite + line-budget check. Reviews target a 24-48h window during the work week.

---

## Adding a new specialist agent

See [`CLAUDE.md` → "Adding a new built-in agent"](CLAUDE.md#adding-a-new-built-in-agent). Short version:

1. `agents/{slug}.py` with a `run(payload) -> dict` function and module docstring.
2. Mint UUIDv5 in `server/builtin_agents/constants.py`.
3. Wire into `BUILTIN_INTERNAL_ENDPOINTS` and `_execute_builtin_agent()`.
4. Add spec entry in `server/builtin_agents/specs_part1.py` or `specs_part2.py`.
5. Return structured error envelope on failure: `{"error": {"code": "...", "message": "..."}}`.
6. Handle the no-LLM case — if you fetch real data then synthesize, fall back to raw retrieval if the LLM call fails.

**Agents earn their place by doing something Claude can't do in chat.** Real APIs, live fetches, sandboxed execution. LLM-prompting wrappers will be rejected.

---

## Reporting security issues

Do not open a public issue. Email **security@aztea.ai** instead. See [`SECURITY.md`](SECURITY.md).

---

## Code of conduct

By participating, you agree to follow the [Contributor Covenant](CODE_OF_CONDUCT.md).
