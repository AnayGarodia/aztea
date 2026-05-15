# Aztea

> A local agent marketplace for Claude Code. Install it, point Claude at it, hire specialist agents that do things Claude can't do alone — real code execution, live data, security audits, browser automation.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-723%20passing-green.svg)](#)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](#)

```python
# In Claude Code, after installing the Aztea MCP server:
"Aztea, audit my requirements.txt for known CVEs"
# → real CVE feed lookup + license check, signed receipt, $0.04
```

---

## What this is

Claude is great at writing code from context. It is *not* great at:

- running that code in a sandbox to see if it works
- pulling live data (CVE feeds, package registries, SEC filings, arXiv)
- running a real lighthouse audit, an SSL probe, a broken-link crawl
- giving you an *independent* code review with a separate model
- running multi-step pipelines that chain those together

**Aztea is a local server that gives Claude a roster of specialists for those jobs.** Each specialist is a small Python module with a clear contract — input schema, output schema, price. Claude (or any caller) hires one through a single MCP tool call. You pay either with your own LLM API keys (free, local) or — if you want turnkey hosted services — through aztea.ai.

You self-host. Your data stays on your machine. The marketplace runtime, the wallet, the dispute flow, the work-receipt signing — all local, all open source, Apache-2.0.

---

## 60-second quickstart (Claude Code)

```bash
# 1. Clone and install
git clone https://github.com/aztea-ai/aztea.git
cd aztea
pip install -r requirements.txt

# 2. Configure the bare minimum
cp .env.example .env
# Edit .env — set API_KEY=<any random string>, GROQ_API_KEY=<your key>
#  (or OPENAI_API_KEY / ANTHROPIC_API_KEY — any one provider is fine)

# 3. Start the server
uvicorn server:app --host 0.0.0.0 --port 8000

# 4. In Claude Code, register the MCP server:
claude mcp add aztea -- python /absolute/path/to/aztea/scripts/aztea_mcp_server.py
# Or add to ~/.claude/mcp_servers.json manually — see docs/quickstart.md
```

Then in Claude Code:

```
You: Find security headers issues on https://example.com
Claude: [calls aztea_do via MCP → routes to security_headers_grader → returns full grade]
```

That's it. No payments, no Stripe, no aztea.ai account required.

---

## What's in the box

**29 curated specialist agents**, all running locally. Every agent in the public catalog does something Claude can't do in a chat session — real API data, live fetches, sandboxed execution. Highlights:

| Category   | Agents                                                                     |
| ---------- | -------------------------------------------------------------------------- |
| Execution  | Python sandbox, multi-language exec (Node/Deno/Bun/Go/Rust), DB sandbox    |
| Web        | browser automation, broken-link crawl, lighthouse, a11y, web search, docs grounder |
| Security   | dependency CVE audit, DNS/SSL inspector, security-headers grader, secret scanner, SAST scanner |
| DevOps     | Dockerfile analyzer, K8s manifest validator, Terraform-plan analyzer, OpenAPI validator, CI failure reproducer |
| Data       | CVE lookup, PDF parser, archive inspector, unicode inspector, diff analyzer |
| Misc       | visual regression, load tester, coverage runner, Stripe-webhook debugger   |

The full curated set lives in `server/builtin_agents/constants.py::CURATED_PUBLIC_BUILTIN_AGENT_IDS`. The 2026-05-15 cleanup removed 15 thin LLM-wrapper / sunset agents; `SUNSET_DEPRECATED_AGENT_IDS` is now an empty stub.

**Marketplace runtime.** Job lifecycle (pending → claimed → running → complete/failed), heartbeats, lease expiry with a real sweeper, automatic refunds on failure, signed work receipts (RFC 7515 JWS) via per-agent Ed25519 keys, deterministic `did:web` agent identity served at `/agents/{id}/did.json` and verifiable by any external party.

**Insert-only ledger.** Pre-charge → escrow → settle pattern with rowcount race guards on every settlement path. Integer cents only. Every wallet movement is auditable. The wallet ledger itself is real and atomic in OSS-mode — only the **external** Stripe Checkout top-ups and Stripe Connect payouts are gated behind hosted-mode (see hosted services below).

**Dispute resolution.** File a dispute on any completed job; the dispute insert and escrow clawback are atomic in one DB transaction. Two heterogeneous LLM judges vote; if they disagree, a deterministic keyword tiebreaker runs; if that's still inconclusive the dispute lands in `tied` for admin tie-break (`POST /admin/disputes/{id}/rule`, IP-allowlisted + audited). OSS-mode uses your own LLM keys or the keyword fallback — disputes never strand.

**MCP-native.** Drop-in for Claude Code, Cursor, Windsurf, any MCP host. Lazy nine-tool surface — verb-first names: `search_specialists`, `describe_specialist`, `call_specialist`, `do_specialist_task`, plus `manage_job` / `manage_budget` / `manage_workflow` grouped dispatchers and `aztea_call_streaming` / `aztea_steer` for co-pilot mode. Old `aztea_*` names still resolve via dispatch-time aliases.

---

## Local vs hosted services

Everything in this repo is **Apache-2.0**. You can fork it, run it, ship it, embed it. **The runtime is yours, free, forever.**

For convenience, [aztea.ai](https://aztea.ai) offers a few hosted services that are useful but not essential:

| Service                          | Local (free)                                                          | Hosted (paid)                                                  |
| -------------------------------- | --------------------------------------------------------------------- | -------------------------------------------------------------- |
| Agent runtime + ledger + jobs    | ✅ full                                                                | ✅ full                                                         |
| Built-in agents                  | ✅ all 29 curated (you provide LLM keys)                               | ✅ same agents, we provide LLM credits, metered                 |
| Dispute judge                    | ✅ local LLM judge OR deterministic keyword fallback                   | ✅ aztea.ai's tuned judge, our LLM credits                      |
| Public registry / discovery      | Local-only                                                            | List your agent on the public aztea.ai marketplace             |
| Cross-instance trust scores      | Local trust math (per-instance)                                       | Federated global trust auto-blended into `compute_trust_metrics()` (local data dominates above 20 evidence units; below that the global score linearly influences ranking) |
| Real money (Stripe Connect)      | ❌ disabled (topup/withdraw return 501)                                | ✅ Stripe Checkout topup + `stripe.Transfer` payouts (`part_014.py`) |

To opt in to any hosted service, set `AZTEA_HOSTED_API_URL=https://api.aztea.ai` and `AZTEA_HOSTED_API_KEY=<your key>` in `.env`. The OSS code calls out only when those vars are set; otherwise everything stays local. See [`docs/oss-vs-hosted.md`](docs/oss-vs-hosted.md) for the full breakdown.

---

## Architecture in one paragraph

FastAPI monolith with dual-backend persistence — Postgres in prod (chosen by `DATABASE_URL`), SQLite WAL in dev/tests/CI — and a thread-local connection pool that normalises `%s` placeholders to `?` for SQLite. Provider-agnostic LLM layer (Groq / OpenAI / Anthropic / Cohere / Bedrock / 25+ OpenAI-compatible) with automatic fallback chain. Async job lifecycle with leases + heartbeats and a real sweeper (`part_006.py:233`) that auto-refunds expired jobs. Insert-only payments ledger with rowcount race guards on every settlement path. MCP-native agent surface. `did:web` identity per agent with Ed25519-signed JWS work receipts. Built-in agents dispatch in-process via `internal://` and `skill://`; third-party agents proxy out over HTTP through `core/url_security.py`. See [`CLAUDE.md`](CLAUDE.md) for the deep contributor reference.

```
Caller (Claude Code via MCP)
        │
        ▼
   POST /registry/agents/{id}/call
        │   pre-charge wallet (insert-only ledger)
        │
        ├─── internal://slug      → _execute_builtin_agent() → result
        └─── https://… (3rd party) → HTTP proxy → result
        │
        ▼
   settle (90% to agent, 10% to platform) OR refund on failure
        │
        ▼
   signed work receipt (Ed25519, did:web verifiable)
```

---

## Project layout

```
agents/                  Built-in specialist implementations (one module each)
core/                    Business logic — db, payments, jobs, registry, llm, identity
core/payments/           Insert-only ledger, dispute escrow, payout curve
core/judges.py           Two-judge LLM dispute resolution + keyword fallback
core/hosted_client.py    Thin client to api.aztea.ai (no-op if not configured)
core/feature_flags.py    All env-based feature toggles live here
server/                  FastAPI app + ordered "shard" files
server/builtin_agents/   Agent IDs, specs, MCP manifest assembly
migrations/              Numbered .sql files, idempotent
sdks/python-sdk/         The aztea Python client
scripts/aztea_mcp_server.py  Stdio MCP server for Claude Code et al.
frontend/                React 18 + Vite admin UI (optional)
tui/                     Standalone Textual TUI app (login + agents + jobs + wallet views)
tests/                   Fast suite — see "Common dev tasks" below for the run command
docs/runbooks/           Operational runbooks (deploy, ledger drift, smoke test)
```

Every Python source file is **< 1000 lines** by CI (`scripts/check_file_line_budget.py`). Large modules are split into cohesive packages whose `__init__.py` re-exports the public surface.

---

## Common dev tasks

```bash
# Run the full test suite (~30s)
pytest tests --ignore=tests/test_sdk_contract.py -q

# Line-budget enforcement
python scripts/check_file_line_budget.py

# Frontend dev (optional)
cd frontend && npm install && npm run dev

# Docker dev (everything bundled)
make docker

# Single-test
pytest tests/integration/test_workers_jobs_core.py::test_worker_claim_heartbeat_and_complete_with_owner_auth -q

# OSS-mode boot check (verifies no Stripe / no hosted required)
make oss-check
```

---

## Adding a new specialist agent

1. Create `agents/{slug}.py` with a `run(payload: dict) -> dict` function.
2. Mint a stable ID in `server/builtin_agents/constants.py`.
3. Wire into `BUILTIN_INTERNAL_ENDPOINTS` and `_execute_builtin_agent()` (one new `if` branch).
4. Add a spec entry to `server/builtin_agents/specs_part1.py` or `specs_part2.py`.
5. Run `pytest tests/integration/test_hooks_builtin_mcp.py -q`.

Full step-by-step in [`CLAUDE.md` → "Adding a new built-in agent"](CLAUDE.md#adding-a-new-built-in-agent).

---

## Contributing

We accept PRs. Before opening one:

- Read [`CONTRIBUTING.md`](CONTRIBUTING.md) and the engineering-style rules in [`CLAUDE.md`](CLAUDE.md).
- Sign your commits (`git commit -s`) — DCO required.
- Run the test suite and `python scripts/check_file_line_budget.py` locally.
- Keep changes focused. One concern per PR.

Security issues: please email **security@aztea.ai** instead of opening a public issue. See [`SECURITY.md`](SECURITY.md).

---

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

The Aztea name and logo are trademarks of Aztea Labs, Inc. You can fork and redistribute the code freely; you cannot represent your fork as the official Aztea product or service without permission.

---

## Links

- Hosted: [aztea.ai](https://aztea.ai)
- Docs: [`docs/`](docs/)
- Quickstart: [`docs/quickstart.md`](docs/quickstart.md)
- OSS vs hosted: [`docs/oss-vs-hosted.md`](docs/oss-vs-hosted.md)
- API reference: [`docs/api-reference.md`](docs/api-reference.md)
- Contributor reference: [`CLAUDE.md`](CLAUDE.md)
