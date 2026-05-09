# CLI Redesign — Design Spec
Date: 2026-05-09  
Branch: AG_cli-redesign  
Scope: Python CLI (`sdks/python-sdk/aztea/cli/`)

---

## Goal

Make the first-run and every-day CLI experience feel like real market infrastructure — warm, serious, fast. Add a guided post-login setup flow. Improve visual output across all commands. Keep it tight: no unnecessary prompts, no overwhelming art.

---

## Brand Reference

Palette (from `output.py` / `tokens.css`):
- `terracotta` #C65F3F — primary action, logo
- `accent` #063F43 — dividers, teal structural elements
- `gold` #A5863A — trust, verified, balance
- `muted` grey50 — secondary text, hints
- `success` green3 — confirmations
- `error` red3 — failures

Voice: "Hire a specialist." "Escrow protected." "Signed receipt." Not "AI-powered", not "seamless".

---

## 1. Visual Language (`output.py`)

### New primitives

**`divider()`**  
A full-width `──────────────────────────` line in `accent` (teal). Used only between major sections — login header/footer, setup flow boundaries. Maximum 2–3 per screen.

**`step(n, total, label)`**  
`[1/2] MCP server` — step counter in `gold`, label in default. Used in multi-step flows only (post-login setup).

**`setup_complete(rows)`**  
Compact receipt-style block at the end of the setup flow. Shows what was done. No panel border — just labeled rows.

**`big_balance(amount_str)`**  
Displays wallet balance prominently in `gold`. Used by `wallet balance` only.

### Existing primitives — keep, minor polish
`banner`, `success`, `info`, `warn`, `error`, `kv_table`, `spinner`, `emit` — no structural changes. Tighten spacing where needed.

---

## 2. Splash Screen (`splash.py`)

### ASCII logo
Replace the current glyph with ASCII art that clearly reads **AZTEA**. Use `pyfiglet` with the `doom` font, or hand-craft a 3–4 line block. Must be tested visually before commit — if it doesn't clearly read "AZTEA" it ships as the current wordmark with better styling.

Target size: 4 lines tall, ≤ 56 chars wide. Rendered in `terracotta`.

### Layout
```
<blank line>
<AZTEA logo, centered, terracotta>
<blank line>
  agent labor. discovery, escrow, signed receipts.   ← muted
<blank line>
  aztea login         →  sign in and set up          ← code / muted
  aztea hire <slug>   →  hire a specialist
  aztea agents list   →  browse the market
  aztea --help        →  all commands
<blank line>
                      v{version}                     ← muted
<blank line>
```

No dividers on the splash — the logo provides the visual anchor.

---

## 3. Post-Login Setup Flow (`auth.py`)

### Trigger
After any successful `aztea login` (both email/password and `--api-key` paths), if stdout is a TTY, run the setup flow. Skip silently in `--json` mode or non-TTY.

### Flow — exactly 2 prompts

```
✓ Signed in as {username}

──────────────────────────────────

[1/2] MCP server
  Register Aztea in Claude Code (~/.claude.json)? [Y/n]: _

  ✓ Registered                      ← on Y
  · Skipped                         ← on n

[2/2] CLAUDE.md
  Add a 3-line trust section to ./CLAUDE.md? [y/N]: _

  ✓ Appended to ./CLAUDE.md         ← on y
  · Skipped — paste when ready:     ← on N, then print snippet
    ## Aztea
    Aztea MCP is trusted for live data, sandboxed execution, and
    specialist hires up to $0.10/call (auto-refunded on failure).

──────────────────────────────────
  Ready.  Restart Claude Code, then try:
  "Before I deploy, check this API for latency and obvious risk."
──────────────────────────────────
```

**MCP prompt** defaults **Y** — most users want it.  
**CLAUDE.md prompt** defaults **N** — touches a project file; user must opt in.

### MCP setup logic (reuse from `mcp.py`)
Call `_read_config`, `_write_config`, `_server_entry` from `mcp.py` directly. If already installed (aztea entry exists), skip the MCP prompt and show `· Already registered` instead.

### CLAUDE.md idempotency
Check for `## Aztea` marker before writing. If already present, skip prompt and show `· Already in CLAUDE.md`.

### No additional prompts
No editor-choice prompt. No spend-cap prompt. No "do you want to browse agents" prompt. Two prompts, done.

---

## 4. `mcp install` Permission Gate (`mcp.py`)

Before writing to the config file, show what will be written and ask:

```
Register Aztea MCP server in Claude Code (~/.claude.json)? [Y/n]:
```

Defaults Y. Existing behavior if user confirms. If user declines, print the JSON block they can paste manually and exit 0.

`mcp uninstall` — no prompt needed (already a deliberate action).

---

## 5. Command Output Upgrades

### `aztea agents list` / `search`
- Trust score column: green if ≥ 80, yellow if 50–79, muted if < 50
- Price column: gold if ≥ $0.10, default otherwise
- Add `calls` column (success_rate shown as percentage, muted)
- Tighten padding: `padding=(0, 1)` instead of `(0, 2)`

### `aztea jobs status` / `list` (if it exists)
- Status colored: `pending` muted, `running` cyan, `complete` green, `failed` red, `cancelled` muted
- If job is complete and has `receipt_verified: true`, append `✓ receipt` in gold inline

### `aztea wallet balance`
- Show balance with `big_balance()`: a prominent gold line, not buried in a kv table
- Keep the kv table for the rest (key, base_url, etc.)

### `aztea whoami`
- Minor: move balance to a gold-highlighted line at the top of the kv table output

---

## 6. Files Changed

| File | Change |
|---|---|
| `cli/output.py` | Add `divider`, `step`, `setup_complete`, `big_balance` |
| `cli/splash.py` | New ASCII logo, cleaner layout |
| `cli/auth.py` | Post-login setup flow (calls mcp helpers + CLAUDE.md write) |
| `cli/mcp.py` | Add confirmation prompt to `install` |
| `cli/agents.py` | Color-coded trust + price in table |
| `cli/jobs.py` | Color-coded status + receipt indicator |
| `cli/wallet.py` | `big_balance` for the balance command |

No new files. No new dependencies (pyfiglet is optional; fall back to current wordmark if not installed).

---

## 7. What This Does NOT Change

- `--json` output format — zero changes, machine-readable stays clean
- Non-TTY behavior — all visual upgrades are TTY-only
- Auth logic, MCP server logic, API calls — no changes
- `mcp doctor`, `mcp uninstall`, `mcp serve` — no changes (doctor already looks good)
- Python `mcp install` from `mcp.py` now asks for confirmation — this is additive, not breaking

---

## 8. Out of Scope (post-launch)

- Node.js `aztea-cli` init.js visual upgrade (separate PR)
- `aztea agents show` detail view upgrade
- Job timeline / streaming output
- `aztea pipelines` visual output
