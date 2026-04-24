# Bug Fixes + Agent Analytics Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four frontend bugs (stale closure, three missing 375px mobile passes) and add a collapsible per-agent analytics panel to MyAgentsPage.

**Architecture:** All changes are frontend-only. The analytics data is already returned by existing API endpoints (`/registry/agents/mine` for call/latency/completion stats, `/wallets/me/agent-earnings` for revenue). The AgentRow component is restructured from a `<motion.button>` (whole row navigates) to a `<motion.div>` wrapper containing a keyboard-accessible navigation area and a sibling expand `<button>`, with an `AnimatePresence` panel below.

**Tech Stack:** React 18, motion/react (AnimatePresence, motion.div), Lucide icons, CSS custom properties from `src/theme/tokens.css`, existing UI primitives (`Stat`, `Skeleton`, `Badge`, `Button`, `EmptyState`).

---

## File Map

| File | What changes |
|---|---|
| `frontend/src/features/onboarding/OnboardingWizard.jsx` | Wrap `dismiss` in `useCallback`; fix `keydown` effect deps |
| `frontend/src/pages/WalletPage.css` | Add `@media (max-width: 480px)` breakpoint |
| `frontend/src/pages/AgentDetailPage.css` | Add `@media (max-width: 480px)` breakpoint |
| `frontend/src/pages/JobDetailPage.css` | Add `@media (max-width: 480px)` breakpoint |
| `frontend/src/ui/Stat.css` | Add `stat--warn` variant (follows existing pattern) |
| `frontend/src/pages/MyAgentsPage.jsx` | Parallel earnings fetch, restructured `AgentRow`, analytics panel |
| `frontend/src/pages/MyAgentsPage.css` | Styles for row-wrap, row-header, expand-btn, panel, panel-grid |

---

## Task 1: Fix OnboardingWizard stale closure

**Files:**
- Modify: `frontend/src/features/onboarding/OnboardingWizard.jsx`

**Problem:** `dismiss` is defined after the `keydown` useEffect but referenced inside it. The effect's dep array lists `[visible, storageKey]` — `dismiss` itself is missing, so the effect captures a stale closure. If `storageKey` changes between renders, the effect calls the old `dismiss` which writes to the wrong localStorage key.

- [ ] **Step 1: Add `useCallback` to imports**

Open `frontend/src/features/onboarding/OnboardingWizard.jsx`. Change line 1:

```js
import { useState, useEffect, useCallback } from 'react'
```

- [ ] **Step 2: Move `dismiss` above the keydown effect and wrap in `useCallback`**

Find the block starting at line 184 (the `keydown` useEffect) and the `dismiss` declaration at line 193. Replace both with the corrected version:

```js
  const dismiss = useCallback(() => {
    if (storageKey) localStorage.setItem(storageKey, '1')
    setVisible(false)
  }, [storageKey])

  useEffect(() => {
    if (!visible) return undefined
    const onKeydown = (event) => {
      if (event.key === 'Escape') dismiss()
    }
    window.addEventListener('keydown', onKeydown)
    return () => window.removeEventListener('keydown', onKeydown)
  }, [visible, dismiss])
```

`dismiss` is now stable per `storageKey`, and the effect always closes over the current version.

- [ ] **Step 3: Verify the build passes**

```bash
cd frontend && npm run build 2>&1 | tail -5
```

Expected: `✓ built in` with no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/features/onboarding/OnboardingWizard.jsx
git commit -m "fix(onboarding): wrap dismiss in useCallback to prevent stale closure"
```

---

## Task 2: WalletPage 375px mobile pass

**Files:**
- Modify: `frontend/src/pages/WalletPage.css`

**Problem:** The `wallet__tx-row` grid is `auto 1fr auto auto` (4 columns: icon, memo+date, badge, amount). At 980px it becomes `auto 1fr auto` but that still pushes the 4th child (amount) to a new row. At 375px the layout is broken — amount wraps underneath the badge.

**Fix:** At 480px: collapse to `auto 1fr auto`, hide the `Badge` (sign color already signals credit/debit), ensure memo and date stay stacked (they already are — they're both `<p>` inside a `<div>`).

- [ ] **Step 1: Append the 480px breakpoint to WalletPage.css**

At the bottom of `frontend/src/pages/WalletPage.css`, after the existing `@media (max-width: 980px)` block, add:

```css
@media (max-width: 480px) {
  .wallet__scroll { padding: 16px 12px; }
  .wallet__header { padding: 20px; }
  .wallet__balance { font-size: 2rem; }
  .wallet__tx-row {
    grid-template-columns: auto 1fr auto;
    gap: 8px;
  }
  .wallet__tx-row > .badge { display: none; }
  .wallet__tx-amount { font-size: 0.875rem; }
}
```

Note: `.badge` is the class applied by the `<Badge>` component (see `frontend/src/ui/Badge.css`). The Badge sits as the 3rd child of `.wallet__tx-row` in the DOM; hiding it collapses the grid to 3 items: icon, memo+date, amount — exactly `auto 1fr auto`.

- [ ] **Step 2: Verify build**

```bash
cd frontend && npm run build 2>&1 | tail -5
```

Expected: no errors.

- [ ] **Step 3: Manual check — resize browser to 375px on /wallet**

Confirm: icon visible, memo and date stack normally, amount right-aligned, no badge chip visible, no horizontal scroll.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/WalletPage.css
git commit -m "fix(wallet): add 480px mobile breakpoint for tx-row layout"
```

---

## Task 3: AgentDetailPage 375px mobile pass

**Files:**
- Modify: `frontend/src/pages/AgentDetailPage.css`

**Problem:** The stats grid (`agent-detail__stats-grid`) goes to `1fr` single-column at 560px — fine — but there is no 480px pass. At 375px the stat values (e.g. `9.6 / 10`) stay at `0.9375rem` which is readable, but the hero padding and trust section overflow slightly.

**Fix:** Add a 480px pass ensuring the hero and stats stay within viewport.

- [ ] **Step 1: Append the 480px breakpoint to AgentDetailPage.css**

Find the existing `@media (max-width: 560px)` block (around line 411) and add a new block after it:

```css
@media (max-width: 480px) {
  .agent-detail__scroll { padding: 16px 12px; }
  .agent-detail__hero { padding: 16px; }
  .agent-detail__stats-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  .agent-detail__stat { padding: 8px 10px; }
  .agent-detail__stat-value { font-size: 0.875rem; }
  .agent-detail__inline-stats { flex-direction: column; gap: 8px; }
}
```

- [ ] **Step 2: Verify build**

```bash
cd frontend && npm run build 2>&1 | tail -5
```

Expected: no errors.

- [ ] **Step 3: Manual check — resize to 375px on any agent detail page**

Confirm: stats show in 2×N grid, values not clipped, no horizontal scroll.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/AgentDetailPage.css
git commit -m "fix(agent-detail): add 480px mobile pass for stats grid"
```

---

## Task 4: JobDetailPage 375px mobile pass

**Files:**
- Modify: `frontend/src/pages/JobDetailPage.css`

**Problem:** The info row layout (`job-detail__info-row`) is `display: flex` with `gap: 16px`. The label has `min-width: 140px` (corrected from what the breakpoint says — the CSS shows 140px at default, 100px at 780px). At 375px with 140px label + 16px gap, the value column only gets ~219px, and long values like job IDs break awkwardly.

**Fix:** At 480px, stack label above value (`flex-direction: column`, remove min-width).

- [ ] **Step 1: Append the 480px breakpoint to JobDetailPage.css**

Find the existing `@media (max-width: 780px)` block (near the end of the file) and add after it:

```css
@media (max-width: 480px) {
  .job-detail__scroll { padding: 16px 12px; }
  .job-detail__info-row {
    flex-direction: column;
    gap: 4px;
    padding: 10px 0;
  }
  .job-detail__info-label {
    min-width: unset;
    font-size: 0.75rem;
  }
  .job-detail__info-value {
    font-size: 0.8125rem;
  }
}
```

- [ ] **Step 2: Verify build**

```bash
cd frontend && npm run build 2>&1 | tail -5
```

Expected: no errors.

- [ ] **Step 3: Manual check — resize to 375px on any job detail page**

Confirm: label stacks above value, long mono values (job IDs) wrap within viewport, no horizontal scroll.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/JobDetailPage.css
git commit -m "fix(job-detail): add 480px mobile pass, stack info label above value"
```

---

## Task 5: Add `stat--warn` variant to Stat component

**Files:**
- Modify: `frontend/src/ui/Stat.css`

**Why:** The analytics panel uses the `Stat` primitive with a `warn` variant for 30d completion rate of 60–79%. The Stat component supports `variant` as a string class suffix but `stat--warn` is not in the CSS. Adding it is a natural extension of the existing `stat--positive` / `stat--negative` pattern. No JSX change needed.

- [ ] **Step 1: Add `stat--warn` to Stat.css**

Open `frontend/src/ui/Stat.css`. After the existing:

```css
.stat--accent .stat__value { color: var(--accent); }
.stat--positive .stat__value { color: var(--positive); }
.stat--negative .stat__value { color: var(--negative); }
```

Add:

```css
.stat--warn .stat__value { color: var(--warn); }
```

- [ ] **Step 2: Verify build**

```bash
cd frontend && npm run build 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/ui/Stat.css
git commit -m "feat(ui): add stat--warn color variant"
```

---

## Task 6: Add analytics CSS to MyAgentsPage

**Files:**
- Modify: `frontend/src/pages/MyAgentsPage.css`

**Why:** Do CSS before JSX so the styles are in place when the component is wired up. The current `.myagents__row` is a grid on a `<motion.button>`. After the JSX refactor in Task 7, `.myagents__row` becomes a flex child inside `.myagents__row-header`. Update the CSS to match.

- [ ] **Step 1: Replace the row block and add new classes**

In `frontend/src/pages/MyAgentsPage.css`, find the block starting with `.myagents__row {` and replace everything from that rule through `.myagents__row-chevron { ... }` with:

```css
/* ── Row wrapper (owns the border) ──────────────────────────── */
.myagents__row-wrap {
  border-bottom: 1px solid var(--border);
}
.myagents__row-wrap:last-child { border-bottom: none; }

/* ── Row header: navigation area + expand toggle ─────────────── */
.myagents__row-header {
  display: flex;
  align-items: stretch;
}

.myagents__row {
  display: grid;
  grid-template-columns: 36px 1fr auto;
  gap: 14px;
  align-items: center;
  padding: 14px 0 14px 16px;
  background: transparent;
  border: none;
  text-align: left;
  cursor: pointer;
  color: inherit;
  font-family: var(--font-sans);
  flex: 1;
  min-width: 0;
  transition: background 0.1s ease;
  border-radius: 0;
}
.myagents__row:hover,
.myagents__row:focus-visible { background: var(--surface-2); }
.myagents__row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

/* ── Expand toggle ───────────────────────────────────────────── */
.myagents__expand-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  flex-shrink: 0;
  background: transparent;
  border: none;
  border-left: 1px solid var(--border);
  cursor: pointer;
  color: var(--text-muted);
  transition: color 0.15s ease, background 0.1s ease;
}
.myagents__expand-btn:hover { background: var(--surface-2); color: var(--text-primary); }
.myagents__expand-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.myagents__expand-icon {
  transition: transform 0.2s var(--ease);
  flex-shrink: 0;
}
.myagents__expand-icon--open { transform: rotate(180deg); }

/* ── Analytics panel ─────────────────────────────────────────── */
.myagents__panel {
  border-top: 1px solid var(--border);
  overflow: hidden;
}
.myagents__panel-inner {
  padding: var(--sp-4) var(--sp-5);
  background: var(--surface-2);
}
.myagents__panel-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--sp-3) var(--sp-4);
}
.myagents__panel-error {
  font-size: 0.8125rem;
  color: var(--negative);
  padding: var(--sp-2) 0;
}
.myagents__panel-skeleton {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--sp-3) var(--sp-4);
}
```

Also keep all existing `.myagents__row-icon`, `.myagents__row-main`, `.myagents__row-name`, `.myagents__row-desc`, `.myagents__row-reason`, `.myagents__row-tags`, `.myagents__row-tag`, `.myagents__row-meta`, `.myagents__row-price` rules unchanged — only the `.myagents__row` grid and `.myagents__row-chevron` rules change.

- [ ] **Step 2: Update the 640px breakpoint**

Find the existing `@media (max-width: 640px)` block and replace with:

```css
@media (max-width: 640px) {
  .myagents__scroll { padding: 20px 16px; }
  .myagents__row { grid-template-columns: 36px 1fr; }
  .myagents__row-meta { display: none; }
  .myagents__panel-grid,
  .myagents__panel-skeleton { grid-template-columns: repeat(2, 1fr); }
}
```

- [ ] **Step 3: Verify build**

```bash
cd frontend && npm run build 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/MyAgentsPage.css
git commit -m "feat(my-agents): add analytics panel CSS and restructure row grid"
```

---

## Task 7: Refactor AgentRow and wire analytics panel in MyAgentsPage

**Files:**
- Modify: `frontend/src/pages/MyAgentsPage.jsx`

This is the main JSX change. It restructures `AgentRow`, adds the analytics panel, and parallelises the earnings fetch.

- [ ] **Step 1: Update imports**

Replace the import block at the top of `frontend/src/pages/MyAgentsPage.jsx`:

```jsx
import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'motion/react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Skeleton from '../ui/Skeleton'
import Stat from '../ui/Stat'
import EmptyState from '../ui/EmptyState'
import Reveal from '../ui/motion/Reveal'
import { fetchMyAgents, fetchAgentEarnings } from '../api'
import { useAuth } from '../context/AuthContext'
import { Plus, Bot, ExternalLink, ChevronDown } from 'lucide-react'
import './MyAgentsPage.css'
```

Changes from original: added `Stat`, added `fetchAgentEarnings`, replaced `ChevronRight` with `ChevronDown`, removed `ChevronRight`.

- [ ] **Step 2: Add helper functions above `AgentRow`**

Add these pure helpers between the `fmtUsd` function and the `AgentRow` component:

```jsx
const prefersReducedMotion =
  typeof window !== 'undefined' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

function completionVariant(rate) {
  if (rate === null || rate === undefined) return ''
  if (rate >= 0.8) return 'positive'
  if (rate >= 0.6) return 'warn'
  return 'negative'
}

function fmtCompletion(rate) {
  if (rate === null || rate === undefined) return '--'
  return `${Math.round(rate * 100)}%`
}

function fmtLatency(sec) {
  if (sec === null || sec === undefined) return '--'
  return `${sec}s`
}
```

- [ ] **Step 3: Replace the `AgentRow` component**

Replace the entire `AgentRow` function with:

```jsx
function AgentRow({ agent, earnings, onClick }) {
  const [open, setOpen] = useState(false)

  const tags = Array.isArray(agent.tags)
    ? agent.tags
    : (typeof agent.tags === 'string' ? JSON.parse(agent.tags || '[]') : [])
  const status = agent.status ?? 'active'
  const isProblematic = status === 'suspended' || status === 'banned'

  const earnedCents = earnings?.total_earned_cents ?? null
  const earnedFmt = typeof earnedCents === 'number'
    ? '$' + (earnedCents / 100).toFixed(2)
    : '--'

  return (
    <motion.div
      className="myagents__row-wrap"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: prefersReducedMotion ? 0 : 0.2 }}
    >
      <div className="myagents__row-header">
        {/* Navigation area */}
        <div
          className="myagents__row"
          role="button"
          tabIndex={0}
          onClick={onClick}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onClick() }}
        >
          <div className="myagents__row-icon">
            <Bot size={15} color="var(--accent)" />
          </div>
          <div className="myagents__row-main">
            <p className="myagents__row-name">{agent.name}</p>
            <p className="myagents__row-desc">{agent.description}</p>
            {isProblematic && agent.suspension_reason && (
              <p className="myagents__row-reason">{agent.suspension_reason}</p>
            )}
            {tags.length > 0 && (
              <div className="myagents__row-tags">
                {tags.slice(0, 4).map(t => (
                  <span key={t} className="myagents__row-tag">{t}</span>
                ))}
              </div>
            )}
          </div>
          <div className="myagents__row-meta">
            <Badge label={status} variant={STATUS_VARIANT[status] ?? 'default'} dot />
            <span className="myagents__row-price">{fmtUsd(agent.price_per_call_usd)} / call</span>
          </div>
        </div>

        {/* Expand toggle */}
        <button
          className="myagents__expand-btn"
          onClick={(e) => { e.stopPropagation(); setOpen(o => !o) }}
          aria-label={open ? 'Hide analytics' : 'Show analytics'}
          aria-expanded={open}
          type="button"
        >
          <ChevronDown
            size={14}
            className={`myagents__expand-icon${open ? ' myagents__expand-icon--open' : ''}`}
          />
        </button>
      </div>

      {/* Collapsible analytics panel */}
      <AnimatePresence>
        {open && (
          <motion.div
            className="myagents__panel"
            key="panel"
            initial={prefersReducedMotion ? false : { height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={prefersReducedMotion ? undefined : { height: 0, opacity: 0 }}
            transition={{ duration: prefersReducedMotion ? 0 : 0.25, ease: [0.16, 1, 0.3, 1] }}
          >
            <div className="myagents__panel-inner">
              <div className="myagents__panel-grid">
                <Stat
                  label="Total calls"
                  value={agent.total_calls ?? '--'}
                />
                <Stat
                  label="30d completion"
                  value={fmtCompletion(agent.job_completion_rate)}
                  variant={completionVariant(agent.job_completion_rate)}
                />
                <Stat
                  label="Median latency"
                  value={fmtLatency(agent.median_latency_seconds)}
                />
                <Stat
                  label="Revenue earned"
                  value={earnedFmt}
                  variant={typeof earnedCents === 'number' && earnedCents > 0 ? 'positive' : ''}
                />
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
```

- [ ] **Step 4: Update `MyAgentsPage` to parallel-fetch earnings and pass them to `AgentRow`**

In the `MyAgentsPage` default export, replace the state and `load` callback:

```jsx
export default function MyAgentsPage() {
  const { apiKey } = useAuth()
  const navigate = useNavigate()
  const [agents, setAgents] = useState([])
  const [earningsMap, setEarningsMap] = useState({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    if (!apiKey) return
    setLoading(true)
    setError(null)
    try {
      const [agentsData, earningsData] = await Promise.all([
        fetchMyAgents(apiKey),
        fetchAgentEarnings(apiKey),
      ])
      setAgents(agentsData?.agents ?? [])
      const map = {}
      for (const row of (earningsData?.earnings ?? [])) {
        map[row.agent_id] = row
      }
      setEarningsMap(map)
    } catch (err) {
      setError(err?.message || 'Failed to load agents.')
    } finally {
      setLoading(false)
    }
  }, [apiKey])

  useEffect(() => { load() }, [load])
```

- [ ] **Step 5: Pass `earnings` prop to `AgentRow` and remove `AnimatePresence` wrapper that was on the list**

In the render section, find where `AgentRow` is rendered and update:

```jsx
{agents.map(agent => (
  <AgentRow
    key={agent.agent_id}
    agent={agent}
    earnings={earningsMap[agent.agent_id] ?? null}
    onClick={() => navigate(`/agents/${agent.agent_id}`)}
  />
))}
```

Remove the `<AnimatePresence>` wrapper that previously wrapped the list (animation is now per-row inside `AgentRow`). The containing `<div className="myagents__list">` stays.

- [ ] **Step 6: Verify build**

```bash
cd frontend && npm run build 2>&1 | tail -20
```

Expected: clean build with no errors or warnings about undefined variables.

- [ ] **Step 7: Manual smoke test — full golden path**

1. Log in and navigate to `/worker/agents` (My Agents page).
2. Confirm agent rows render with the Bot icon, name, description, badge, price.
3. Click the `▾` expand button on any row — analytics panel slides open.
4. Confirm four stat chips appear: Total calls, 30d completion (colored appropriately), Median latency, Revenue earned.
5. Click `▾` again — panel closes smoothly.
6. Click the main row area (anywhere except the expand button) — navigates to agent detail page. Press Back.
7. Tab through the row using keyboard — confirm the main area and expand button are both focusable independently.
8. Resize to 375px — confirm panel grid collapses to 2 columns, meta (badge/price) hidden in row, expand toggle stays visible.
9. Confirm no horizontal scroll at 375px.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/pages/MyAgentsPage.jsx
git commit -m "feat(my-agents): collapsible per-agent analytics panel with parallel earnings fetch"
```

---

## Task 8: Final integration commit

- [ ] **Step 1: Run the frontend build one last time**

```bash
cd frontend && npm run build 2>&1 | tail -5
```

Expected: `✓ built in` with no errors.

- [ ] **Step 2: Run the backend test suite to confirm no regressions**

```bash
cd /path/to/project && pytest tests --ignore=tests/test_sdk_contract.py -q 2>&1 | tail -5
```

Expected: `231 passed, 1 skipped`.

- [ ] **Step 3: Final summary commit**

```bash
git log --oneline -8
```

You should see the 6 commits from Tasks 1–7. If all is clean, optionally create a summary commit:

```bash
git commit --allow-empty -m "feat: bug fixes + agent analytics dashboard complete"
```

---

## Self-Review

**Spec coverage:**
- ✅ OnboardingWizard stale closure → Task 1
- ✅ WalletPage 375px → Task 2
- ✅ AgentDetailPage 375px → Task 3
- ✅ JobDetailPage 375px → Task 4
- ✅ Stat--warn variant → Task 5 (needed for Task 7 analytics)
- ✅ Analytics panel CSS → Task 6
- ✅ AgentRow refactor + parallel fetch + AnimatePresence panel → Task 7
- ✅ Full build + test verification → Task 8

**Placeholder scan:** No TBDs, no "similar to Task N", no vague steps. All code blocks are complete.

**Type consistency:** `fetchAgentEarnings` returns `{ earnings: [{ agent_id, agent_name, total_earned_cents, call_count, last_earned_at }] }` (confirmed from `api.js` comment at line 602). `earningsMap[agent_id]` is therefore `{ total_earned_cents, ... } | undefined`. The `earnings` prop in `AgentRow` is `earnings?.total_earned_cents ?? null` — consistent with the null-guard in `earnedFmt`. `agent.total_calls`, `agent.job_completion_rate`, `agent.median_latency_seconds` are all present on the `/registry/agents/mine` response (confirmed in `_agent_response` in `server/application_parts/part_002.py`). All field names match.
