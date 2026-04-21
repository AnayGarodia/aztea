# Work Examples — Agent Detail Page Enhancement

**Date:** 2026-04-21  
**Status:** Approved  
**Scope:** Frontend only — `AgentDetailPage.jsx` + `AgentDetailPage.css`

---

## Goal

Surface the ring-buffered work examples already stored per agent so visitors see real proof-of-work before hiring. The existing "Work portfolio" section has the right structure but needs polish: relative timestamps, input/output truncation, stagger entry animation, a 5-example cap, and a better empty state.

---

## What Already Exists (no changes needed)

- **Backend endpoint:** `GET /registry/agents/{agent_id}/work-history` (server.py:8240) — paginated, auth-gated, returns `{ items, total, limit, offset }`.
- **API helper:** `fetchAgentWorkHistory(key, agentId, { limit, offset })` in `src/api.js`.
- **Section skeleton:** "Work portfolio" card in `AgentDetailPage.jsx` (lines 331–461) with expand/collapse per item, AnimatePresence height animation, rating and quality chips, artifact listing.
- **CSS:** `.agent-detail__portfolio-*` block in `AgentDetailPage.css`.

---

## Changes

### `AgentDetailPage.jsx`

**1. Fetch limit: 10 → 5**  
Change `loadWorkHistory` call from `limit: 10` to `limit: 5`. Remove the "Load more" button entirely. The ring buffer holds up to 20; we show the 5 most recent.

**2. Relative timestamp helper**  
Add a `relativeTime(isoString)` function (no external dependency):
```
< 1 min   → "just now"
< 1 hour  → "Xm ago"
< 24 hrs  → "Xh ago"
< 7 days  → "X days ago"
≥ 7 days  → "X weeks ago"
```
Replace `toLocaleDateString(...)` in the timestamp chip.

**3. Input truncation**  
Replace `JSON.stringify(ex.input, null, 2)` full dump with:
- Collapsed state: extract a preview string from the input dict by checking keys `prompt`, `query`, `text`, `input` in that order; fall back to `JSON.stringify` of the whole dict if none match. Truncate to 200 chars, show with "…" + an inline "expand" toggle button.
- Expanded state: full `JSON.stringify` as before.
- State tracked per-item via a `Set` in a `useState` (separate from `expandedExample` which controls the accordion).

**4. Output truncation**  
In the collapsed accordion body, show a plain-text summary of the output:
- If output has a `text`, `summary`, `result`, or `answer` string field: truncate to 200 chars.
- Otherwise: `JSON.stringify` truncated to 200 chars.
- Same expand toggle pattern as input.

**5. Stagger entry animation**  
Wrap the `.agent-detail__portfolio-list` div with the existing `<Stagger>` component (`staggerDelay={0.07}`, `delayStart={0.05}`). Each portfolio item becomes a direct child so it gets a stagger variant.

**6. Section rename**  
Change header label from "Work portfolio" to "Recent Work".

**7. Empty state copy**  
Change from:  
`"No public work examples yet. Invoke this agent to generate examples."`  
To:  
`"No public work examples yet — be the first to hire this agent."`

**8. Quality indicator**  
The existing `qualityScore` and `rating` chips remain. No changes needed — they already show a subtle quality indicator when present.

### `AgentDetailPage.css`

Add:
- `.agent-detail__portfolio-summary` — `font-size: 0.8125rem; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%;`
- `.agent-detail__portfolio-expand-link` — inline text button, `font-size: 0.75rem; color: var(--accent); background: none; border: none; cursor: pointer; padding: 0; margin-left: 4px;`

---

## What Is NOT Changing

- Backend ring buffer logic (`_record_public_work_example`, `_AGENT_WORK_EXAMPLES_MAX`)
- The `/work-history` endpoint or `fetchAgentWorkHistory`
- Artifact rendering
- AnimatePresence accordion animation on expand
- ResultRenderer (still used in expanded output for full-fidelity rendering)
- Page layout / section order

---

## Data Shape (reference)

Each item from `/work-history`:
```json
{
  "job_id": "...",
  "input": { ... },
  "output": { ... },
  "created_at": "2026-04-18T10:23:00Z",
  "latency_ms": 1420,
  "rating": 4,
  "quality_score": 3,
  "model_provider": "groq",
  "model_id": "llama-3.1-70b",
  "artifacts": []
}
```

---

## Success Criteria

- [ ] Section shows ≤ 5 examples; no "load more"
- [ ] Timestamps are relative ("3 days ago"), not absolute
- [ ] Long inputs and outputs are truncated at 200 chars with an expand toggle
- [ ] Cards stagger in on scroll (respects `prefers-reduced-motion`)
- [ ] Empty state shows the new copy
- [ ] No new colors, fonts, or hardcoded values — CSS variables only
- [ ] No backend changes
