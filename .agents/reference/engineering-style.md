# Engineering style — agents must follow

> Resolved reference for `CLAUDE.md`. Read before writing or refactoring any code.
> The hard CI rules are summarised in `CLAUDE.md` → "Non-negotiables"; this is the full set.

These rules apply to every change in this repo, no exceptions. Some are CI-enforced; others are reviewer-enforced. None are optional.

## Hard rules (CI enforces — do not bypass)

- **File length:** hard limit 1000 lines. Soft warn at 500 for new files. If a new file crosses 500, split before adding more. `scripts/check_file_line_budget.py` enforces the hard limit.
- **Function length:** max ~80 statements, cyclomatic complexity ≤ 10. Smaller is better — decompose aggressively. If a function needs scrolling to understand, it's too long; a long function is a refusal to think about abstraction.
- **Catch blocks:** never empty, never bare `except:`. Either handle the error explicitly (with structured logging) or re-raise. Empty catches and vague `console.log` calls hide bugs across sessions.
- **No silent fallbacks.** Every except path either logs structured error context with the actual exception, or re-raises. Don't swallow. `except Exception: pass`, `|| defaultValue` without a comment, and `?.` chains that swallow None are all banned unless the silence is explicitly documented with a reason.
- **Magic numbers:** name them. Money, ratio, timeout, and limit constants live as module-level `UPPER_SNAKE` with a one-line comment. If a literal appears more than once, or its meaning isn't self-evident from immediate context, it gets a name. Allowlist: `0`, `1`, `-1`, `2`, simple HTTP status codes.
- **Money paths:** never `float()` in `core/payments/` or in any settlement code. Integer cents only. CI greps for floats in money modules.

## Soft rules (you must follow — reviewer will flag)

- **Trace every caller in the same change.** When you alter a function signature, grep the codebase and update every caller in the same diff. Partial changes that compile but leave the codebase inconsistent are worse than no change. Never defer this.
- **Never leave a task half-done.** If a refactor needs 12 call-site edits, do all 12 in one change. A half-applied refactor is more harmful than not starting.
- **Search before creating.** Before adding a utility, helper, or formatter, grep the codebase first. Duplicates are a tax we already pay (`fmtDate` lives in 10+ files because someone skipped this).
- **Re-read after writing.** After writing code, re-read the full file. Match the file's existing style and patterns, not your defaults.
- **Boy scout rule.** When you touch old code, leave it slightly better — a clearer name, a removed redundancy, a tightened comment, a deleted dead branch. Compounded across a year, this is the only realistic way the codebase stays navigable.
- **Dead code is deleted, not commented out.** Git is the undo button. Commented-out code is noise that erodes trust in the file.
- **New functionality = new test, same commit.** A function with no test is a function with an unknown contract. Tests and implementation ship together or not at all.
- **Comment WHY, never WHAT.** Well-named identifiers describe what the code does. Comment a non-obvious constraint, an invariant, a workaround for a specific bug, behavior that would surprise a reader. `# Sorts the list` is worthless; `# Sorted insertion required — downstream consumers assume monotonicity` is not. Never reference the current task ("added for issue #123") — that belongs in the PR description.
- **Add a comment before touching unclear code.** When existing code's intent isn't clear, write the explanation first (in a comment), then change the code. The comment survives the next session.
- **TODOs carry a ticket and a date.** `# TODO` with no context is a broken promise. `# TODO(2026-05-09): remove once API v2 sunset — see issue #412` is a commitment.
- **Prefer explicit over implicit.** Avoid magic numbers, default-parameter tricks, and behavior that depends on call order. Every assumption should be visible in the code, not inferred from context.
- **Simplest code that solves the problem.** Clever code that requires inference will be misread in a future session. Three similar lines beat a premature abstraction.

## Design preferences

- **Pure functions where possible.** A function that takes inputs and returns outputs, with no side effects, is trivially testable, movable, and understandable later. If a function can be pure, it must be — side effects require explicit justification in a comment. Push side effects to the edges (HTTP routes, DB writes, filesystem).
- **Fail loudly, fail early.** Validate inputs at function boundaries and raise immediately. Never let bad data propagate three stack frames before dying with an inscrutable error.
- **No boolean parameters.** `render(page, True)` is unreadable at the call site. Use enums, named constants, or split into two functions.
- **Consistent return types.** A function that returns either a list or None is two functions pretending to be one. Pick a contract and hold it; use `Optional` explicitly when absence is meaningful.
- **Mutations are local or documented.** If a function modifies its argument in place, the name must say so (`sort_inplace`, `normalize_records`) or the docstring must flag it. Surprise mutation is a bug waiting to happen.
- **Imports at top of file** unless lazy-loading is the explicit intent. Top-of-file imports make the dependency graph legible; buried imports hide coupling.
- **Log at boundaries, not inside logic.** Logging belongs at I/O entry/exit points, not scattered through computation. Logs inside pure logic are a sign the function is doing too much.
- **One-way dependencies.** Business logic in `core/` must not import from `server/routes/`, `frontend/`, or HTTP/transport layers. The arrow goes outward, never inward.
- **Make illegal states unrepresentable.** Use enums, discriminated unions, and types that exclude invalid combinations rather than scattering defensive runtime checks. A pydantic model with strict literals beats four `if status not in {...}: raise`.
- **One thing per function.** A function should do one thing, and that thing should be obvious from its name without reading the body. If you need a comment to explain _what_ it does, it's doing too much.
- **Configuration ≠ code.** Secrets, env-specific values, and feature flags change for different reasons, on different schedules, by different people. They belong in `.env` and `core/feature_flags.py` — never inlined.
- **Document non-trivial modules in agent-optimized format.** Every Python module with business logic and every non-trivial React component must have a structured block at the top — no narrative prose. Use exactly these four fields (omit any that would be empty):

  ```python
  # OWNS: what this module is responsible for
  # NOT OWNS: what it explicitly does NOT own (prevents scope creep)
  # INVARIANTS: hard rules — an agent must never violate these
  # DECISIONS: non-obvious choices that CAN be changed if the reason no longer holds
  # KNOWN DEBT: broken or suboptimal things — fix when you touch this
  ```

  `INVARIANTS` = never touch. `DECISIONS` = understand before changing, but you're allowed. `KNOWN DEBT` = actively encouraged to fix. This distinction matters: an agent must not freeze on broken code because a comment made it look intentional.

## Operational rules

- **Never delete migrations.** Add new ones with the next sequence number.
- **Never force-push main.** Always create a new commit.
- **Never open raw `sqlite3.connect()` or `psycopg2.connect()`.** Use `core/db.py` exclusively — it owns backend selection and exception exports for both.
- **Frontend errors must be inline.** Toasts for success only; inline error state for failures.
- **Keep operational runbooks current.** When you add a feature that touches money, runtime dependencies, or a buyer surface, update the relevant runbook in `docs/runbooks/` in the same commit.
