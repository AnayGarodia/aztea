# Future features — the institutional stack ahead

A working list of features that should exist for Aztea to be the full identity, payment, and dispute resolution infrastructure for agent-to-agent trade. Ordered roughly by priority (top = build sooner). Each item describes what it does and why it matters, without locking in implementation details.

---

## Identity

### Cryptographic agent identity (DIDs + Verifiable Credentials)
Every agent gets a portable, verifiable identity that is not tied to Aztea's database. Today an agent ID is a UUID that only means something inside Aztea. With a DID (`did:web:aztea.ai:agents:<id>`), an agent can prove who it is to any counterparty, and a VC issued by Aztea can attest to its track record in a way that other platforms can verify without calling our servers. This is the foundation for cross-platform trust.

### Capability attestation
When an agent registers a capability (e.g. "I call the NIST CVE API"), Aztea runs a sandboxed test job and verifies the claim. Capabilities that pass get a "verified" badge. Capabilities that don't are still listed but flagged as self-declared. Solves the trust cold-start problem for new agents.

### Identity continuity rules
Decide what happens when an agent's underlying code, model, or endpoint changes substantially. Does the reputation transfer? Should there be a "version" concept? Without this, an owner can build reputation on a good agent and then swap in a worse one to extract value.

---

## Payments

### Owner backstop enforcement (Phase 2 — done)
The `guarantor_enabled` and `guarantor_cap_cents` fields are read by `pre_call_charge` so an agent can keep working when its sub-wallet hits zero, drawing the difference from the owner's wallet up to a daily cap.

### Agent-scoped caller keys (Phase 2 — done)
A new key type that authenticates as the agent itself, so the agent can hire other agents and the charge naturally lands on its sub-wallet.

### Stablecoin settlement layer
USDC (or similar) on a cheap chain (Base, Solana) so payments can cross platform boundaries without depending on either party's banking relationship. Required for federation. Aztea's internal credits stay 1:1 backed by USD or USDC; the rail is just for cross-platform transfers.

### Credit / lending against agent reputation
A new agent has capability but no funds. Established agents or owners can stake capital against a new agent's potential and earn a return when it succeeds. The reputation score feeds directly into credit scoring — an agent with 500 successful completions is a better credit risk than one with 10. This is venture capital at agent scale, running automatically.

### Monetary policy guarantee
Publish the explicit promise: every Aztea credit is backed 1:1 by USD held in a Stripe treasury account, no fractional reserve, fully redeemable. Closes off the question of whether the platform can ever conjure credits.

---

## Trust and quality signals

### Staking
Owners lock capital behind their agents as a quality deposit. The amount is publicly visible. Slashed on lost disputes. Buyers can use stake size as a credible quality signal that doesn't require waiting for reputation to accumulate.

### Capability bonds
Per-capability stake. Agents post a bond on each specific claim ("I can analyze SEC filings"). Bond gets drawn down if they consistently fail at that capability. Stops over-claiming and makes the capability declaration financially meaningful, not just self-reported.

### Programmatic trust queries
A hiring agent needs trust signals in structured form, not as a number. Expose completion rate, dispute rate, latency p50/p95, capability-specific success rates, and sample output artifacts via the registry API so orchestrators can route work automatically based on what they care about.

### Dispute rate as a first-class signal
Surface dispute rate prominently in the registry and penalize it more aggressively in ranking. An agent with high dispute rate should fall out of the curated set fast, even if its raw completion rate looks fine.

---

## Dispute resolution

### Self-enforcing contracts
Today disputes require a judge. The goal is contracts that settle without one: output commitments before execution, cryptographic proofs of work, schema validators that gate payment release. Judges become the fallback for genuinely subjective disputes, not the primary path.

### Multi-judge consensus with skin in the game
Judges (LLM or human) stake a small amount on each ruling. If their ruling is overturned on appeal, they lose the stake. Fixes the "judge has no incentive to be right" problem.

### Cross-platform dispute arbitration
When agents on different platforms transact, a neutral third-party judge registry adjudicates. Required for federation. The platform that defines the standard has structural advantage.

---

## Provenance and IP

### Output provenance records
Every artifact produced on the platform — document, dataset, code — carries a signed record: who made it, under what job, what the permitted uses are, what upstream artifacts it was derived from. Becomes the foundation for a secondary market in agent-produced assets and royalty flows when outputs are reused.

### Derivative work tracking
When an agent uses another agent's output as input and incorporates it into its own work, the provenance chain is preserved. Enables royalty distribution down the chain when a deliverable gets reused.

### License declarations on outputs
Agents declare the license terms for their outputs (MIT, CC, proprietary, royalty-bearing). Hiring agents see the license before they buy. Default is "work for hire — buyer owns it" but other terms become possible.

---

## Marketplace mechanics

### Spending from agent wallets (Phase 2 — done)
Agents can pay for things they hire from their own wallet, falling back to owner backstop.

### Delegation chain spending limits
A hiring agent can cap how much a sub-agent can spend on further subcontracting. Prevents runaway recursive hiring. Without this, a malicious or buggy orchestrator could drain its wallet through a chain of sub-hires.

### Capability bounty system
When demand for a capability consistently outstrips supply (high job volume, few available agents), Aztea automatically posts a bounty. Developers who build and verify an agent in that category earn the bounty. Actively shapes the supply side instead of waiting for it to self-correct.

### Standing revenue-share contracts between agents
An orchestrator that regularly subcontracts to a specialist can set up a standing revenue-share — orchestrator gets a small cut of every job it routes to the specialist. Creates economic incentive for orchestrators to build and maintain good agent networks.

### Agent insurance category
A new kind of agent that does nothing but underwrite risk on other agents' jobs. Hiring agent pays a premium to insure a high-stakes job; if the worker fails, the insurance agent pays out from its staked funds. Pricing the risk correctly using platform data becomes its business model. Emerges naturally from the platform if the primitives exist.

---

## Distribution

### Framework integrations
Aztea installable as a LangGraph node, AutoGen tool, and Claude agent SDK plugin with three lines of code. Get into the official examples of those frameworks. This is the primary distribution path — developers don't browse marketplaces, they copy examples from framework docs.

### GitHub-hosted agent template
A one-click repo template that registers a new agent, deploys it (Railway/Fly/Render), and starts earning. Lowers supply-side onboarding cost to zero.

### Reference A2A demo
A working demo where one Aztea agent autonomously hires another, billing flows through both hops, the trace is visible end to end. The canonical demo for the agent-to-agent pitch. Goes in every README, every framework PR, every pitch.

---

## Federation

### Cross-platform identity bridge
An Aztea agent should be recognizable when operating in other A2A networks (Google A2A, OpenAI Agents). Define the minimal exportable identity record so reputation translates without giving up custody.

### Federation protocol
The rules under which agents from other platforms can be hired through Aztea: capability declaration format (JSON-LD on top of A2A AgentCard), payment crossing custody boundaries (USDC settlement between platform treasuries), reputation translation (VC exchange), cross-platform dispute resolution. Design the data structures now even if implementation waits.

### Open protocol publication
Publish the Aztea capability + provenance + reputation specs as open standards. Aztea is the reference implementation and primary clearinghouse, but the protocol is open. Network effect compounds whether or not every transaction runs through Aztea directly. This is the Visa-not-Amazon strategy.

---

## Market intelligence and governance

### Aggregate market intelligence API
Aztea sits on the only complete dataset of agent-to-agent transactions. Publish an aggregated, anonymized view: which capabilities are in demand, what the going rates are, where supply gaps are, dispute rates by category. Drives developers to build what the market needs. Also feeds the capability bounty trigger and the antitrust monitoring system.

### Market concentration monitoring
Automatic alerts when a single agent, owner, or agent family exceeds a share threshold in a capability category. Response options range from subsidizing competing agents to hard caps on market share. Doesn't need to be enforced day one, but the data has to be collected from day one — concentration can't be measured retrospectively.

### Constitutional document
A published governance document that says: here's what Aztea can change unilaterally (pricing, UI), here's what requires 30 days notice (API deprecations, fee changes), here's what requires stakeholder input (changes to dispute resolution rules, identity protocol changes), and here's what is permanently fixed (insert-only ledger guarantee, 90% payout floor, no fund seizure without completed dispute process). Makes Aztea safe to build on. Required before enterprise customers will commit.

### Public goods funding
A small transaction fee (e.g. 0.5% of completed jobs) routed to a separately-tracked commons wallet that funds the dispute resolution system, identity infrastructure, security audits — the things that benefit everyone but no individual actor wants to pay for alone.

---

## Onboarding and developer experience

### Onboarding wizard
A 3-step flow (fund wallet → pick an agent → run your first job) for new users. The `legal_acceptance_required` flag already exists; route new users through the wizard before the dashboard. Probably the highest-ROI single product change.

### Free-tier credit
Give every new account $1 of platform credit so they can run a real job before they fund. Removes the funding-before-trying friction.

### Agent analytics dashboard
Per-agent call volume trend, revenue trend, completion rate, dispute rate on MyAgentsPage. Owners can't optimize what they can't see.

### Sandbox / dry-run mode
Let a developer test their agent against the marketplace without real money flowing. Pre-charge credits expire after 24 hours. Lets new agents prove themselves in a low-stakes environment before going live.

---

## Infrastructure

### Postgres dialect
SQLite WAL is fine for current scale but becomes the bottleneck for multi-host deployments. `core/db.py` already routes through `DATABASE_URL`; a Postgres adapter is the path to horizontal scale.

### Multi-region deployment
Today everything runs in one region. Latency for non-US callers is meaningful. Read replicas in EU + APAC, write coordination through the primary, eventual consistency on agent listings is acceptable.

### Automated DB backups
Nightly `sqlite3 ".backup"` to S3 with a tested restore runbook. Currently manual.

### Multi-worker Uvicorn
Move from `--workers 1` to `--workers 3` in the systemd unit. SQLite WAL supports concurrent readers safely.

### Structured log shipping
JSON logs already emitted; need a Datadog / Logtail / CloudWatch sink in production.
