# AGENTS.md — Universal AI Brief for Aztea

Read this file first. Then read `.agents/` files relevant to your task. Full dev rules are in `CLAUDE.md` — read it before touching money, migrations, or auth.

---

## What this is

Aztea is the **identity, payment, and dispute-resolution layer for agent-to-agent commerce** — Stripe + Upwork + Dun & Bradstreet, but participants are software.

Two horizons drive every decision:
- **Local goal (now):** ship something individual developers want today — fast hires, deterministic tools, transparent spend, verifiable receipts.
- **Global goal (north star):** open infrastructure where any agent on any platform can hire, pay, trust, and settle with any other agent. Federation, portable reputation, stablecoin settlement.

Every PR is graded against both. See `.agents/VISION.md` for the full philosophy.

---

## Architecture in one sentence

FastAPI monolith on SQLite WAL, provider-agnostic LLM layer, async job lifecycle, insert-only ledger, MCP-native agent surface, `did:web` identity per agent.

---

## Critical invariants — these must never be violated

**Money:**
- Integer cents only. No floats in the ledger. Ever.
- `transactions` table is INSERT-only. Corrections = compensating entries.
- `pre_call_charge` / `post_call_payout` / `post_call_refund` each have a rowcount race guard. Replicate it on every new settlement path.
- Dispute insert + escrow clawback must happen in ONE SQLite transaction.
- `wallets.balance_cents` is a cache — update it in the same transaction as the ledger row.

**Database:**
- Use `core/db.py` exclusively. Never open `sqlite3.connect()` directly.
- Never hold a write lock during an HTTP call to a downstream agent.
- Never delete or re-use a migration filename.

**Auth:**
- All outbound URLs go through `core/url_security.py`. No exceptions.
- API key values are never logged — only the prefix.

**LLM:**
- Use `raw.text`, never `raw.content` (`.content` silently returns `None`).
- Never pass `model=` to `CompletionRequest` when using `run_with_fallback`.

**Frontend:**
- Errors must be inline. Toasts are for success only.
- Never hardcode colors or spacing — use `src/theme/tokens.css` variables.

---

## File map (quick orientation)

```
server/application_parts/   FastAPI shards (part_000 = imports, part_006+ = routes)
agents/                      Built-in agent implementations
core/                        auth, payments, jobs, registry, disputes, reputation, identity, llm
frontend/src/
  api.js                     All API calls go here
  context/MarketContext.jsx  Global state (agents, wallet, jobs)
  features/                  agents/, jobs/, auth/
  pages/                     One file per route
  ui/                        Design-system primitives (Button, Card, Badge, etc.)
  ui/motion/                 Animation primitives (Reveal, Stagger, NumberMorph, etc.)
  theme/tokens.css            CSS variables for all colors, spacing, typography
sdks/python-sdk/             AzteaClient, AgentServer
tui/                         Textual terminal app
scripts/                     MCP server, CLI shim, smoke test, release script
migrations/                  SQL migrations — never delete, always add new
.agents/                     Internal AI-facing docs (you are here)
```

---

## When to read what

| You're about to… | Read this first |
|---|---|
| Touch any money path | `CLAUDE.md` → "Critical invariants — money" + `core/payments/base.py` docstring |
| Add a new built-in agent | `CLAUDE.md` → "Adding a new built-in agent" checklist |
| Build a frontend component or page | `.agents/STYLE.md` — includes the full tool/skill/MCP guide |
| Pick what to work on | `.agents/TODO.md` |
| Understand the product direction | `.agents/VISION.md` |
| Check what's been shipped | `.agents/ROADMAP.md` or `.agents/SESSIONS.md` |
| Change a migration | `CLAUDE.md` → migrations are idempotent, never deleted |
| Change an auth or MCP route | `CLAUDE.md` → auth & MCP surface invariants |

## Available tools — quick reference

**Skills (invoke before starting the relevant work):**
- `frontend-design` — base design guidance, always available (globally installed)
- `high-end-visual-design` — Aztea's style direction; use for all new UI (installed in `.agents/skills/`)
- `design-taste-frontend` — general anti-slop default (installed in `.agents/skills/`)
- `redesign-existing-projects` — audit + fix existing pages with style debt (installed in `.agents/skills/`)
- `imagegen-frontend-web` — generate design reference images (installed in `.agents/skills/`)
- `full-output-enforcement` — force complete output when Claude truncates (installed in `.agents/skills/`)
- `impeccable` — animate/audit/polish/craft workflow — **NOT YET INSTALLED**: download from impeccable.style

**MCPs (use via tool calls):**
- `mcp__magic__*` — component builder, refiner, inspiration, logo search
- `mcp__stitch__*` — design system create/edit/apply
- `mcp__playwright__*` — screenshot, click, navigate in real browser (verify animations/layout)
- `mcp__aztea__*` — search/describe/call Aztea agents from within Claude
- `mcp__plugin_context7_context7__*` — live library docs (React, Vite, Framer Motion, etc.)

Full decision table for which tool to use when is in `.agents/STYLE.md`.

---

## Working rules for AI sessions

1. Read `CLAUDE.md` end-to-end before any non-trivial change.
2. Match existing style: 4-space Python, type hints, ES modules, PascalCase components, camelCase helpers, kebab-case CSS classes.
3. Every module with business logic needs an OWNS / NOT OWNS / INVARIANTS / DECISIONS / KNOWN DEBT block at the top. No narrative prose.
4. Test the full failure path. Refunds must fire on agent failures. If you change a money path without a test, the PR is incomplete.
5. **Before ending your session:** move completed items in `.agents/TODO.md`, update the status table in `CLAUDE.md` if something shipped, append one line to `.agents/SESSIONS.md`.

Don't ship features that tie to neither the local nor the global goal. Don't add third-party deps without strong reason. Don't touch the deprecated agent set (sunset 2026-07-26).

---

## Build commands

```bash
make dev                          # backend with reload
pytest -q tests --ignore=tests/test_sdk_contract.py   # full test suite
npm --prefix frontend run dev     # frontend dev server
npm --prefix frontend run build   # frontend prod build
python scripts/check_file_line_budget.py   # enforce <1000-line rule
```

Current test status: 453 passed, 1 skipped (intentional, feature-flag gated).
