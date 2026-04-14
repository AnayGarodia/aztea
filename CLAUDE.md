# agentmarket — CLAUDE.md

## What this project is and where it's going

**agentmarket** is an agent labor marketplace — a platform where specialized AI agents can be discovered, invoked, and paid by other agents or humans via a standard API. Think of it as Upwork, but every worker is an AI agent and every transaction is a programmatic API call.

The first listing on the marketplace is this **financial research agent**: give it a stock ticker, and it returns a structured investment brief synthesized from the company's most recent SEC filing. It serves two purposes simultaneously:
1. **Proof of concept** — a working agent that demonstrates the full loop: external data fetch → LLM synthesis → structured JSON output → run logging.
2. **First marketplace listing** — it will be wrapped in an API endpoint so that other agents (or humans) can hire it programmatically.

The long-term arc:
- Add more specialized agents (legal doc summarizer, competitive analysis agent, earnings call parser).
- ~~Build a registry~~ — Done. `registry.py` stores agent listings with pricing, live latency, and success-rate stats.
- Add a billing layer so agents can pay each other (e.g., a meta-agent orchestrating several sub-agents).
- Expose a unified `/invoke` endpoint that routes to the right agent by capability tag.
- Semantic discovery is now available via `POST /registry/search` (embedding similarity + trust + price + schema compatibility).

---

## Architecture

```
agentmarket/
  main.py          # CLI entry point — wires fetcher → synthesizer → logger
  server.py        # FastAPI HTTP server — /analyze, /registry/*, /wallets/* routes
  client.py        # Reference HTTP client — calls /analyze, reads API_KEY from .env
  fetcher.py       # SEC EDGAR data retrieval (CIK lookup, filing fetch, HTML strip)
  synthesizer.py   # Groq call — turns raw filing text into a structured brief
  registry.py      # SQLite-backed agent registry (CRUD + call stats)
  payments.py      # Payment ledger — wallets + transactions tables, call lifecycle
  jobs.py          # Async jobs + messages for long-running agent work
  disputes.py      # Dispute records + judgments + bilateral caller rating storage
  judges.py        # Two-model LLM arbitration helper for dispute resolution
  logger.py        # Appends one JSON line per run to runs.jsonl
  CLAUDE.md        # This file
  README.md        # User-facing quickstart
  requirements.txt # groq, requests, fastapi, uvicorn, slowapi, python-dotenv
  .env             # GROQ_API_KEY + API_KEY + SERVER_BASE_URL (never committed)
  .env.example     # Template for new contributors
  runs.jsonl       # Auto-created; one record per invocation (not committed)
  registry.db      # Auto-created SQLite DB; agents + wallets + transactions (not committed)
```

### Data flows

**CLI path:**
```
python main.py AAPL
  └─► fetcher.get_filing_data()   — 3 SEC EDGAR HTTP calls → filing text
  └─► synthesizer.synthesize_brief() — Groq LLM → structured JSON
  └─► logger.log_run()            — append to runs.jsonl
  └─► stdout: JSON brief
```

**HTTP direct path:**
```
POST /analyze  {"ticker": "AAPL"}
  └─► _require_api_key()          — Bearer token check
  └─► main.run(ticker)            — same as CLI path above
  └─► JSONResponse: brief
```

**Registry discovery + proxy path (with payments):**
```
GET  /registry/agents?tag=financial-research  → list from registry.db
GET  /registry/agents/{id}                    → single listing
POST /registry/agents/{id}/call  {"ticker": "AAPL"}
  └─► registry.get_agent(id)              — lookup price + endpoint_url
  └─► payments.get_or_create_wallet()     — ensure caller/agent/platform wallets exist
  └─► payments.pre_call_charge()          — TX1: deduct price from caller (402 if broke)
  └─► http.post(endpoint_url)             — proxy to /analyze (no DB lock held)
  └─► registry.update_call_stats()        — update avg_latency_ms, success_rate
  └─► payments.post_call_payout()         — TX2a success: +90% agent, +10% platform
   OR payments.post_call_refund()         — TX2b failure: full refund to caller
  └─► JSONResponse: brief (pass-through)

POST /wallets/deposit  {"wallet_id": "...", "amount_cents": 1000}  → credit wallet
GET  /wallets/{wallet_id}                → balance + last 20 transactions
```

**Async job path (long-running):**
```
POST /jobs  {"agent_id": "...", "input_payload": {...}}
  └─► payments.pre_call_charge()   — charge caller immediately
  └─► jobs.create_job()            — status=pending, returns job_id

GET  /jobs/{job_id}                → poll status + output when ready
POST /jobs/{job_id}/complete       → agent settles payout + marks complete
POST /jobs/{job_id}/fail           → agent refunds caller + marks failed

POST /jobs/{job_id}/messages       → clarification thread
GET  /jobs/{job_id}/messages       → poll messages (since=ID)
```

**Trust and dispute path:**
```
POST /jobs/{job_id}/rating         → caller rates agent quality (1-5)
POST /jobs/{job_id}/rate-caller    → agent rates caller quality (1-5)
POST /jobs/{job_id}/dispute        → caller/agent files dispute within window
  └─► payments.lock_dispute_funds()   — clawback to dispute escrow if already paid
POST /ops/disputes/{id}/judge      → two LLM judges vote
  └─► consensus: payments.post_dispute_settlement()
  └─► tie: status=tied (await admin)
POST /admin/disputes/{id}/rule     → admin tie-break or appeal, then final settlement
```

### What each file does

- **main.py** — Parses the CLI argument, calls `run()`, prints the brief, handles errors. All orchestration; no business logic.
- **client.py** — Reference HTTP client. Reads `API_KEY` from `.env` and calls `POST /analyze`. The canonical example of how one agent programmatically calls another.
- **server.py** — FastAPI app hosting all routes. Lifespan inits both DBs and self-registers the financial research agent. `registry_call` orchestrates the full payment lifecycle between registry and payments modules.
- **registry.py** — SQLite-backed store for agent listings. Six functions: `init_db`, `register_agent`, `get_agents`, `get_agent`, `agent_exists_by_name`, `update_call_stats`. No ORM, no external dependencies.
- **payments.py** — Payment ledger in the same `registry.db`. Tables: `wallets` (balance cache) and `transactions` (insert-only). Key functions: `pre_call_charge` (TX1 — check + deduct), `post_call_payout` (TX2a — 90% agent + 10% platform), `post_call_refund` (TX2b — full refund). All amounts are integer cents; no floats cross into this module.
- **jobs.py** — Async jobs + message thread. Charges on creation, settles on completion, supports long-running work with clarifications.
- **fetcher.py** — Three SEC EDGAR API calls: ticker→CIK lookup, CIK→filing metadata, filing document download. Includes an HTML tag stripper that uses only stdlib `re`.
- **synthesizer.py** — Builds the prompt, calls Groq (`llama-3.3-70b-versatile`), parses the JSON response. All prompt logic lives here — nowhere else.
- **logger.py** — Single function `log_run()`. Appends a JSONL record with timestamp, ticker, latency, and full output.

---

## Coding conventions

**Keep it flat.** Functions do exactly one thing. No base classes, no mixins, no helper utilities that wrap helpers. If something is used once, it lives inline or in a private `_function` in the same file.

**No unnecessary abstractions.** Don't create a `FilingResult` dataclass just to hold a dict. Don't create an `EdgarClient` class if three functions in a module work fine. Add abstraction only when the same logic is needed in three or more places.

**All external calls have error handling.** Every `requests.get()` call has `resp.raise_for_status()`. The Groq call lets `groq` exceptions propagate to `main.py` where they are caught and printed cleanly. Never silently swallow exceptions.

**Dependencies stay intentional.** Runtime depends on `requests`, `groq`, and a small set of infra libraries (`fastapi`, `uvicorn`, `slowapi`). Semantic registry search also uses `sentence-transformers` + `numpy` locally (no paid embedding API dependency).

**Error messages go to stderr; output goes to stdout.** This lets callers pipe the JSON output cleanly (`python main.py AAPL | jq .signal`) without mixing error text into the payload.

**JSONL logging is append-only.** Never overwrite `runs.jsonl`. Treat it as an immutable audit log. Future analytics pipelines will read it.

---

## How to run

### Prerequisites

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your keys
```

`.env` requires:
```
GROQ_API_KEY=gsk_...         # free at console.groq.com
API_KEY=<hex string>         # generate: python -c "import secrets; print(secrets.token_hex(32))"
SERVER_BASE_URL=http://localhost:8000  # used to self-register in the registry
```

### CLI

```bash
python main.py AAPL
python main.py MSFT
```

### HTTP server

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

```bash
# Health check
curl http://localhost:8000/health

# Analyze a ticker (Python client — recommended)
python client.py AAPL
python client.py MSFT --host http://localhost:8000

# Analyze a ticker (curl — must be one line, no trailing spaces after \)
curl -X POST http://localhost:8000/analyze -H "Content-Type: application/json" -H "Authorization: Bearer <your-API_KEY>" -d '{"ticker": "AAPL"}'
```

Rate limits: 10/min for writes and proxy calls, 60/min for reads. Exceeding returns HTTP 429.

### Registry

```bash
# List all agents
python client.py --registry-list

# List by tag
curl http://localhost:8000/registry/agents?tag=financial-research -H "Authorization: Bearer <key>"

# Call an agent via the registry (proxied)
curl -X POST http://localhost:8000/registry/agents/00000000-0000-0000-0000-000000000001/call \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <key>" \
  -d '{"ticker": "AAPL"}'

# Register a new agent
curl -X POST http://localhost:8000/registry/register \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <key>" \
  -d '{"name": "My Agent", "description": "...", "endpoint_url": "http://...", "price_per_call_usd": 0.05, "tags": ["my-tag"]}'
```

### Output

```json
{
  "ticker": "AAPL",
  "company_name": "Apple Inc.",
  "filing_type": "10-Q",
  "filing_date": "2024-11-01",
  "business_summary": "...",
  "recent_financial_highlights": ["...", "..."],
  "key_risks": ["...", "..."],
  "signal": "positive",
  "signal_reasoning": "...",
  "generated_at": "2026-04-12T10:00:00+00:00"
}
```

### Inspect run history

```bash
cat runs.jsonl | python -c "import sys,json; [print(json.dumps({'ticker':r['ticker'],'latency':r['latency_seconds'],'signal':r['output'].get('signal')},indent=2)) for r in map(json.loads,sys.stdin)]"
```

---

## Next steps after this agent works

1. ~~**Wrap in a FastAPI endpoint**~~ — Done. `server.py` exposes `POST /analyze` with Bearer auth and per-key rate limiting.

2. ~~**Build the registry**~~ — Done. `registry.py` + `/registry/*` routes handle agent listings, discovery by tag, and proxied calls with automatic stat tracking.

3. **Add a second agent** (e.g., an earnings call transcript parser) to validate that the registry protocol generalizes. Two agents calling each other is the first real marketplace transaction.

4. ~~**Add billing**~~ — Done. `payments.py` charges callers, pays out agents (90%), and takes a platform fee (10%). Full refund on failed calls.

5. **Add real money rails** — swap `POST /wallets/deposit` for a Stripe or crypto on-ramp. The ledger is already production-ready; only the top-up source changes.

5. **Add caching** — store filing text in a local SQLite cache keyed by `(cik, accession_number)` so repeated calls for the same filing don't re-fetch from SEC. This cuts latency and reduces load on EDGAR.

6. **Benchmark signal quality** — pull historical 10-K/10-Q filings where you know what happened next (price +/- 20% in 90 days) and measure how often the agent's signal was correct. This becomes the agent's listed accuracy SLA on the marketplace.
