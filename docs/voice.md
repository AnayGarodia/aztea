# Aztea voice

One page. Read it before changing user-facing copy.

## The rule: warm, not jokey

Write like a senior colleague explaining the system to a peer. Specific verbs.
Concrete nouns. No Slack-isms ("You're crushing it!"), no exclamation
cascades, no corporate cheerleading ("Amazing!"), no self-aware humor
("Oops!"). One sentence is usually enough. Two if the next action isn't
obvious.

| Is | Isn't |
|----|-------|
| "Reconciling the ledger…" | "Loading…" |
| "Funds deposited." | "Yay! Funds added to wallet!" |
| "Rate-limited. Retry after 7s." | "Whoa there, slow down!" |
| "Your API key was revoked. Run `aztea login`." | "Authentication failed" |
| "All caught up." | "Nothing to see here folks!" |

## Money, dispute, and security stay precise

These surfaces are *not* in scope for warming. Precision beats personality
when real cents or trust signals are on the line.

**Do not soften:**
- Any string that includes a dollar amount (`$1.00 minimum`, `$X.XX available`).
- Dispute prose ("Dispute filed. Our judges will review it shortly." is the
  exemplar — keep verbatim; don't reach for cleverness).
- Stripe-rejection text (insufficient funds, destination invalid, KYC
  required).
- Withdrawal copy in `WalletPage.jsx`.
- Any error path with `payment.`, `dispute.`, `auth.`, or `stripe.` in
  the error code.
- Security warnings (revoked keys, scope-insufficient, signature failures).

If you're unsure, leave it as the server wrote it. The helper
`formatApiError` in `frontend/src/utils/errorCopy.js` only fills the
*fallback* — server-authored copy always wins.

## Empty states: imperative, suggest the next action

The user is here because their list is empty. Don't just describe the
emptiness — tell them what they can do.

**Pattern:** _\<title: concrete observation\>_ + _\<sub: one-line next action\>_.

- "No agents yet" + "Pick a specialist from the catalog and run your first
  job." — good.
- "No specialists listed yet" — bad (passive, no path forward).

## Loading: describe what's happening

Never `Loading…`. Tell the user what the system is actually doing right now,
in a verb phrase short enough to read at a glance.

- "Reconciling the ledger…" (Dashboard reconciliation runs)
- "Refining with semantic search…" (Agents page search — already the
  exemplar)
- "Fetching job details." (JobDetail loader)

If `EmptyState`'s default `Loading…` shows, it means the caller forgot to
pass `message=`. Fix the caller, not the default.

## Errors: branch by status. Generic copy is a bug.

Inline, not toast. Specific to the *action* the user attempted. The
`formatApiError(err, { action })` helper in
`frontend/src/utils/errorCopy.js` handles the branching — call it from
every catch site:

```js
catch (err) {
  setError(formatApiError(err, { action: "claim job" }).title)
}
```

The helper reads `err.code` first (so the new taxonomy codes from the
backend land on tailored copy), then falls back to status-aware sentences
for 401/402/403/404/409/429/5xx, then to a final
`"Could not <action>. Try again."` only if everything else is missing.

Never write `err?.message ?? "X failed."` again — that path strips
context the server worked to send.

## Toasts: success only

CLAUDE.md rule, repeated here: **toasts are for success**. Errors stay
inline (the user needs to see them next to the field/control that produced
them).

- One sentence.
- Past or present tense, not imperative ("Funds deposited.", not
  "Deposit your funds.").
- Include the object's name/ID when reasonable (`Key "build-bot" created.`
  beats `Key created.`).
- Don't editorialize the success ("That was easy!").

## Before / after examples

**Empty state — WorkerPage:**
- Before: "No open worker jobs" / "Your owned agents currently have no
  pending/running jobs."
- After: "All caught up" / "Your agents have no open jobs right now. New
  work lands here the second a caller hires them."

**Loading — DashboardPage reconciliation:**
- Before: "Loading reconciliation runs…"
- After: "Reconciling the ledger…"

**Error fallback — WorkerPage claim:**
- Before: `setError(err?.message ?? "Claim failed.")`
- After: `setError(formatApiError(err, { action: "claim job" }).title)`

## Out of scope for the voice sweep

These are intentionally left alone — touching them is a stability risk:

- **SDK-internal exception strings** (`sdks/python-sdk/aztea/agent.py`
  signature/handler errors): these are API contracts for SDK consumers,
  not end-user copy.
- **CLI argparse help text**: argparse renders it; the voice doesn't
  apply.
- **Backend log lines**: they're for operators, not users. The
  observability tone (structured, terse, scannable) is different from the
  product tone here.
