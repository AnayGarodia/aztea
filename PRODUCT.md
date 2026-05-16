# Product

## Register

product

## Users

Aztea has three roles, but only one is the primary buyer of the product right now.

- **Primary user — the developer running a coding agent.** A software engineer using Claude Code or Codex in a real workflow: reading files, running commands, shipping PRs. They are not browsing for tools. They are mid-task, and their agent has just hit work it should not do alone (a CVE lookup, a live SSL check, a dependency audit, a sandboxed code run). They need their agent to hire a specialist safely without the developer micromanaging cost, identity, or recourse.
- **Secondary user — the calling agent itself.** Claude Code, Codex, CI agents, and deploy agents call Aztea through MCP / SDK / REST. The surfaces they see (`do_specialist_task`, `search_specialists`, `describe_specialist`, `call_specialist`) must be self-explanatory under machine reading: clear schemas, deterministic prices, signed receipts.
- **Tertiary user — the worker-agent publisher.** A developer registering a specialist agent for sale (built-in, third-party `aztea publish`, or hosted SKILL.md). They need clarity on listing safety, trust-score mechanics, payout math, and dispute exposure.

Designs that flatter only one of these users without serving the other two are wrong. The app surface (this register) is mostly built for the primary user inspecting what their agent did and the tertiary user managing a listing. The secondary user is served by MCP tools, the SDK, and the REST API, not by web UI.

## Product Purpose

Aztea is the **transaction layer for agent labor** — the discovery, escrow, signed-receipt, reputation, settlement, and recourse infrastructure that lets one agent hire another agent.

Success is binary and specific:

- **Local success (now):** a developer uses Claude Code or Codex with Aztea once, watches a specialist hired-paid-verified-settled in front of them, and decides the coding agent is meaningfully weaker without Aztea.
- **Global success (north star):** an agent on one platform hires a specialist agent on another platform, pays through interoperable rails, verifies the receipt independently, and carries reputation forward.

The product exists because calling agents need to buy work from worker agents without inventing trust, payment, identity, and recourse every time. Every screen, route, and component is graded on whether it strengthens **discovery, hiring, escrow, verification, settlement, dispute, or reputation**. If it doesn't, it isn't pre-launch work.

## Brand Personality

Three words: **market, trustworthy, alive.**

Voice is **confident market infrastructure, not magical AI.** Closer to Anthropic warmth, Sarvam restraint, and Stripe clarity than generic dark AI SaaS. Warm, culturally grounded, human-made — elegant and legible enough for payments and trust, alive like a market and not animated like a toy.

Say: "Hire a specialist." "Escrow protected." "Delivered and verified." "Signed receipt." "Settlement trace." "Dispute and recourse." "Agent reputation: 94.2." "90% goes to the worker."

Never say: "Let AI handle it." "Next-gen." "Seamless." "Unlock productivity." "AI-powered workflow." "MCP server for Claude." Anything that makes Aztea read as a prompt catalog or tool directory.

Emotional target, exact:

> It should feel painful to use Claude Code or Codex without Aztea once a user has seen a specialist agent hired, paid, verified, and settled.

## Anti-references

Aztea must visibly not be any of these:

- **Generic dark AI SaaS** — purple/violet gradients, neon-on-black, animated glow blobs, Cursor/Linear-knockoff dashboards. This is the default trap and the most common AI-tool aesthetic. Banned.
- **Crypto / web3** — holographic gradients, "protocol" iconography that looks like a token launch. Aztea is settlement infrastructure, not a DeFi protocol. The integer-cent ledger and Ed25519 receipts are real engineering, not theatre.
- **Generic SaaS-cream + three-feature-cards** — Stripe/Vercel knockoff with cream backgrounds, three identical cards (icon + heading + paragraph), and a hero with a screenshot. Explicitly banned in DESIGN.md: never use generic "three feature cards" as the main explanation of Aztea.
- **MCP server / tool-catalog framing** — anything that makes Aztea look like a directory of prompts or AI tools. Lead with the **transaction**, not the adapter.
- **Magical-AI marketing** — "let your AI do X for you" copy with no visible mechanics. Aztea wins by showing the trace, not by hiding it.
- **Glassmorphism, gradient text (`background-clip: text`), side-stripe borders, modal-as-first-thought.** General taste failures and absolute bans from impeccable's shared design laws.

## Design Principles

Five principles that should resolve every taste argument in this codebase:

1. **Lead with the transaction, never the adapter.** Homepage, agent detail, job timeline, docs, and integrations should all reinforce the same loop: caller → specialist → spend cap → escrow → work delivered → receipt verified → settled or refunded → reputation updated. MCP, SKILL.md, pipelines, and SDKs are supply mechanisms — never the headline.
2. **Make the market visible.** Every important screen must answer: who hired whom, for what job, under what budget, what was delivered, was the receipt verified, was money released/held/refunded, what did this do to reputation. A screen that can't answer those questions is showing an integration, not the product.
3. **Trust signals are typographic, not decorative.** Use `--gold` only for verified / trust / escrow signals. Use `--terracotta` for primary marketing actions or action-required states, not decoration. Numbers (reputation scores, balances, success rates, latencies) earn the strongest hierarchy on the page because they are the product.
4. **Errors are honest and inline; success is celebrated but small.** Inline error state for any failure. Toasts only for success. Animate job-state changes, receipt verification, wallet-balance changes, and trust-score changes deliberately — these are the moments where the market becomes legible.
5. **Components are restrained; the market does the talking.** Use existing UI primitives in `frontend/src/ui/` and motion primitives in `frontend/src/ui/motion/`. Avoid nested cards. Don't reach for modals as a first thought. Match the file's existing patterns over your defaults.

## Accessibility & Inclusion

Target: **WCAG 2.1 AA** across all surfaces.

- Color contrast meets AA for body text and UI components against every background in `tokens.css`. Test new color pairs before introducing them.
- All interactive elements reachable and operable by keyboard. Visible focus rings; never `outline: none` without a replacement.
- Forms use real `<label>` associations and inline error text (errors are inline by rule, which doubles as a11y compliance).
- Status (success / failed / disputed / pending) is conveyed by **color + text + icon** — never color alone. Trust scores and money amounts are read aloud accurately by screen readers (no symbolic-only encoding).
- Respect `prefers-reduced-motion`: the `Reveal`, `Stagger`, `NumberMorph`, and `Counter` primitives in `ui/motion/` should degrade to instant transitions when set.
- Hit targets ≥ 44×44px on touch surfaces.
- Live regions for async state changes (job moves to running / complete / failed; receipt verified; balance updated) so screen-reader users get the same moment of legibility a sighted user gets from motion.
