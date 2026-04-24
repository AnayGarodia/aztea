# Bug Fixes + Agent Analytics Dashboard — Design Spec

_Date: 2026-04-24_
_Status: Approved_

---

## Overview

Two parallel workstreams in one branch:

1. **Four bug fixes** — stale closure in OnboardingWizard, missing 375px mobile passes on WalletPage, AgentDetailPage, and JobDetailPage.
2. **Collapsible analytics panel on MyAgentsPage** — per-agent call volume, completion rate, latency, and revenue, revealed via an expand toggle on each agent row.

No backend changes required. All data is already returned by existing endpoints.

---

## Part 1 — Bug Fixes

### 1a. OnboardingWizard stale closure (`frontend/src/features/onboarding/OnboardingWizard.jsx`)

**Problem:** The `keydown` useEffect captures `dismiss` from outer scope without including it in the dependency array. If `storageKey` changes between renders, the effect holds a stale closure and writes to the wrong localStorage key.

**Fix:** Wrap `dismiss` in `useCallback` with `[storageKey]` as the dependency. The `keydown` effect then lists `dismiss` as its dependency and always sees the current function.

```js
const dismiss = useCallback(() => {
  if (storageKey) localStorage.setItem(storageKey, '1')
  setVisible(false)
}, [storageKey])

useEffect(() => {
  if (!visible) return
  const onKeydown = (e) => { if (e.key === 'Escape') dismiss() }
  window.addEventListener('keydown', onKeydown)
  return () => window.removeEventListener('keydown', onKeydown)
}, [visible, dismiss])
```

---

### 1b. WalletPage mobile 375px (`frontend/src/pages/WalletPage.css`)

**Problem:** Only one breakpoint at 980px. The `wallet__tx-row` 4-column grid clips at 375px.

**Fix:** Add `@media (max-width: 480px)`:
- `wallet__tx-row` → `grid-template-columns: auto 1fr auto`, move date below memo as a block element, hide Badge type chip (redundant with sign color).
- `wallet__grid` already collapses to 1-col at 980px — no change needed there.

---

### 1c. AgentDetailPage mobile 375px (`frontend/src/pages/AgentDetailPage.css`)

**Problem:** Stats grid stays at 4 columns until 560px, then stops. At 375px stat values clip horizontally.

**Fix:** Add `@media (max-width: 480px)`:
- Stats grid: `grid-template-columns: repeat(2, 1fr)`.
- Reduce stat value font-size one step (e.g. `1.25rem` instead of `1.75rem`).
- Trust gauge row: allow wrap.

---

### 1d. JobDetailPage mobile 375px (`frontend/src/pages/JobDetailPage.css`)

**Problem:** Info-label column has `min-width: 100px` — forces horizontal overflow at 375px.

**Fix:** Add `@media (max-width: 480px)`:
- Switch info row to `flex-direction: column` (label stacked above value).
- Remove `min-width` constraint.

---

## Part 2 — Collapsible Analytics Panel on MyAgentsPage

### Component structure

The current `AgentRow` is a `<motion.button>` (whole row = navigate). Nesting a second interactive element inside a `<button>` is invalid HTML. Replace with a `<div>` wrapper:

```
<div class="myagents__row-wrap">
  <div class="myagents__row" role="button" tabIndex={0} onClick={navigate}>
    [icon] [main info] [meta: badge + price] [expand chevron button]
  </div>
  <AnimatePresence>
    {open && (
      <motion.div class="myagents__panel" ...>
        <div class="myagents__panel-grid">
          <Stat label="Total calls" value={...} />
          <Stat label="30d completion" value={...} variant={...} />
          <Stat label="Median latency" value={...} />
          <Stat label="Revenue earned" value={...} variant="positive" />
        </div>
      </motion.div>
    )}
  </AnimatePresence>
</div>
```

The expand `<button>` is inside the row `<div>` (not nested inside a `<button>`), calls `e.stopPropagation()` so the row click doesn't also navigate, and rotates its chevron icon 180° via CSS transition when `open`.

### Data flow

`MyAgentsPage` fetches two sources in parallel on mount:

| Source | Data used |
|---|---|
| `fetchMyAgents(apiKey)` (already exists) | `total_calls`, `job_completion_rate`, `median_latency_seconds`, `dispute_rate` per agent |
| `fetchAgentEarnings(apiKey)` (already in `api.js`, used in WalletPage) | `total_earned_cents`, `call_count` per `agent_id` |

Earnings are joined to agents client-side: `earningsMap = { [agent_id]: row }`.

### Stat display rules

| Stat | Source field | Color variant |
|---|---|---|
| Total calls | `agent.total_calls` | default |
| 30d completion | `agent.job_completion_rate` (null if no jobs) | `positive` ≥0.80, `warn` 0.60–0.79, `negative` <0.60, default if null |
| Median latency | `agent.median_latency_seconds` (null if no jobs) | default; shown as `Xs` or `--` |
| Revenue earned | `earningsMap[id].total_earned_cents` | `positive` if >0, default otherwise |

### Loading & error states

- **Loading:** four `<Skeleton variant="rect" height={52} />` chips in the same 2×2 grid, shown while either fetch is in-flight.
- **Error:** small inline text in `var(--negative)` inside the panel — never a toast (per project error handling rule: toasts for success only).
- **No data:** show `--` for each stat rather than hiding the panel.

### Animation

```js
// motion.div panel
initial={{ height: 0, opacity: 0 }}
animate={{ height: 'auto', opacity: 1 }}
exit={{ height: 0, opacity: 0 }}
transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
style={{ overflow: 'hidden' }}
```

Chevron rotation: CSS `transform: rotate(0deg)` → `rotate(180deg)` with `transition: transform 0.2s var(--ease)`.

`prefers-reduced-motion`: follow the same pattern as `Reveal.jsx` — if `matchMedia('(prefers-reduced-motion: reduce)').matches`, skip animation.

### CSS tokens used (no hardcoded values)

- Background: `var(--surface-2)`
- Border: `var(--border)`
- Text: `var(--text-primary)`, `var(--text-muted)`
- Positive: `var(--positive)`
- Warn: `var(--warn)`
- Negative: `var(--negative)`
- Spacing: `--sp-3`, `--sp-4`, `--sp-5`
- Radius: `var(--r-md)`
- Font: `var(--font-mono)` for numeric values

### Mobile (≤ 640px)

Panel grid stays `repeat(2, 1fr)` — already fits at 375px since stat values are compact. The expand toggle remains visible (it is not in `myagents__row-meta` which is hidden at 640px — it gets its own column in the grid).

At ≤ 640px, update `myagents__row` grid from `36px 1fr auto 20px` to `36px 1fr 28px` — the `auto` meta column is already hidden, and the last slot now accommodates the expand toggle button explicitly.

---

## Files changed

| File | Change |
|---|---|
| `frontend/src/features/onboarding/OnboardingWizard.jsx` | useCallback fix for dismiss |
| `frontend/src/pages/WalletPage.css` | 480px breakpoint |
| `frontend/src/pages/AgentDetailPage.css` | 480px breakpoint |
| `frontend/src/pages/JobDetailPage.css` | 480px breakpoint |
| `frontend/src/pages/MyAgentsPage.jsx` | Collapsible analytics panel, parallel earnings fetch, AgentRow restructured |
| `frontend/src/pages/MyAgentsPage.css` | Panel styles, expand toggle, 640px mobile grid update |

---

## Out of scope

- Backend changes (none needed)
- Sparklines / time-series charts (require extra API endpoints not yet built)
- Notification preferences in SettingsPage (separate future spec)
