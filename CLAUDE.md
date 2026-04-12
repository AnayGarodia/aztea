# agentmarket — CLAUDE.md

## What this project is and where it's going

**agentmarket** is an agent labor marketplace — a platform where specialized AI agents can be discovered, invoked, and paid by other agents or humans via a standard API. Think of it as Upwork, but every worker is an AI agent and every transaction is a programmatic API call.

The first listing on the marketplace is this **financial research agent**: give it a stock ticker, and it returns a structured investment brief synthesized from the company's most recent SEC filing. It serves two purposes simultaneously:
1. **Proof of concept** — a working agent that demonstrates the full loop: external data fetch → LLM synthesis → structured JSON output → run logging.
2. **First marketplace listing** — it will be wrapped in an API endpoint so that other agents (or humans) can hire it programmatically.

The long-term arc:
- Add more specialized agents (legal doc summarizer, competitive analysis agent, earnings call parser).
- Build a registry (agents list themselves with pricing, latency SLAs, input/output schemas).
- Add a billing layer so agents can pay each other (e.g., a meta-agent orchestrating several sub-agents).
- Expose a unified `/invoke` endpoint that routes to the right agent by capability tag.

---

## Architecture

```
agentmarket/
  main.py          # CLI entry point — wires fetcher → synthesizer → logger
  fetcher.py       # SEC EDGAR data retrieval (CIK lookup, filing fetch, HTML strip)
  synthesizer.py   # Claude call — turns raw filing text into a structured brief
  logger.py        # Appends one JSON line per run to runs.jsonl
  CLAUDE.md        # This file
  README.md        # User-facing quickstart
  requirements.txt # groq, requests
  runs.jsonl       # Auto-created; one record per invocation (not committed)
```

### Data flow

```
User: python main.py AAPL
         │
         ▼
main.py: run(ticker)
         │
         ├─► fetcher.get_filing_data(ticker)
         │       ├─ GET https://www.sec.gov/files/company_tickers.json  → CIK
         │       ├─ GET https://data.sec.gov/submissions/CIK{cik}.json → most recent 10-K/10-Q metadata
         │       └─ GET https://www.sec.gov/Archives/edgar/...          → filing HTML → stripped text
         │
         ├─► synthesizer.synthesize_brief(filing_data)
         │       └─ POST https://api.groq.com/... (llama-3.3-70b-versatile)
         │           → returns JSON brief
         │
         └─► logger.log_run(ticker, brief, latency)
                 └─ appends to runs.jsonl
         │
         ▼
stdout: JSON brief
```

### What each file does

- **main.py** — Parses the CLI argument, calls `run()`, prints the brief, handles errors. All orchestration; no business logic.
- **fetcher.py** — Three SEC EDGAR API calls: ticker→CIK lookup, CIK→filing metadata, filing document download. Includes an HTML tag stripper that uses only stdlib `re`.
- **synthesizer.py** — Builds the prompt, calls Claude (`claude-opus-4-6`), parses the JSON response. All prompt logic lives here — nowhere else.
- **logger.py** — Single function `log_run()`. Appends a JSONL record with timestamp, ticker, latency, and full output.

---

## Coding conventions

**Keep it flat.** Functions do exactly one thing. No base classes, no mixins, no helper utilities that wrap helpers. If something is used once, it lives inline or in a private `_function` in the same file.

**No unnecessary abstractions.** Don't create a `FilingResult` dataclass just to hold a dict. Don't create an `EdgarClient` class if three functions in a module work fine. Add abstraction only when the same logic is needed in three or more places.

**All external calls have error handling.** Every `requests.get()` call has `resp.raise_for_status()`. The Groq call lets `groq` exceptions propagate to `main.py` where they are caught and printed cleanly. Never silently swallow exceptions.

**No extra libraries.** `requests` for HTTP, `groq` for LLM inference, stdlib for everything else. HTML stripping uses `re`, not `beautifulsoup4`. JSON is stdlib `json`. This keeps the install footprint minimal and makes the agent easy to package for the marketplace.

**Error messages go to stderr; output goes to stdout.** This lets callers pipe the JSON output cleanly (`python main.py AAPL | jq .signal`) without mixing error text into the payload.

**JSONL logging is append-only.** Never overwrite `runs.jsonl`. Treat it as an immutable audit log. Future analytics pipelines will read it.

---

## How to run

### Prerequisites

```bash
pip install -r requirements.txt
export GROQ_API_KEY=gsk_...
```

### Run

```bash
python main.py AAPL
python main.py MSFT
python main.py NVDA
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

1. **Wrap in a FastAPI endpoint** (`POST /invoke` with `{"ticker": "AAPL"}`) so other agents can call it over HTTP. Add an API key check so only authorized callers can use it.

2. **Write an agent manifest** (`manifest.json`) describing this agent's input schema, output schema, pricing per call, average latency, and capability tags (`["financial-research", "sec-filings", "equity-analysis"]`). This becomes the marketplace listing.

3. **Build the registry** — a simple service that stores manifests and exposes `GET /agents` and `GET /agents/{id}`. A meta-agent can query this to discover what agents exist and what they cost.

4. **Add a second agent** (e.g., an earnings call transcript parser) to validate that the manifest format and invocation protocol generalizes. Two agents working together is the first real marketplace transaction.

5. **Add caching** — store filing text in a local SQLite cache keyed by `(cik, accession_number)` so repeated calls for the same filing don't re-fetch from SEC. This cuts latency and reduces load on EDGAR.

6. **Benchmark signal quality** — pull historical 10-K/10-Q filings where you know what happened next (price +/- 20% in 90 days) and measure how often the agent's signal was correct. This becomes the agent's listed accuracy SLA on the marketplace.
