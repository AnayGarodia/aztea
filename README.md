# agentmarket — Financial Research Agent

An AI agent that fetches the most recent SEC 10-K or 10-Q for any public company and returns a structured investment brief as JSON. This is the first listing on the agentmarket agent labor marketplace.

## Install

```bash
pip install -r requirements.txt
export GROQ_API_KEY=gsk_...
```

## Usage

```bash
python main.py AAPL
python main.py MSFT
python main.py NVDA
```

## Output

```json
{
  "ticker": "AAPL",
  "company_name": "Apple Inc.",
  "filing_type": "10-Q",
  "filing_date": "2024-11-01",
  "business_summary": "Apple designs and sells consumer electronics...",
  "recent_financial_highlights": [
    "Revenue of $94.9B, up 6% YoY",
    "Services revenue grew 12% to $25B"
  ],
  "key_risks": [
    "Concentration risk: ~50% of revenue from iPhone",
    "Regulatory pressure in EU over App Store practices"
  ],
  "signal": "positive",
  "signal_reasoning": "Strong services growth diversifies hardware dependence; balance sheet remains best-in-class.",
  "generated_at": "2026-04-12T10:00:00+00:00"
}
```

Each run is logged to `runs.jsonl` with ticker, timestamp, latency, and full output.

## How it works

1. Looks up the company's CIK on SEC EDGAR using the public company tickers list
2. Finds the most recent 10-K or 10-Q filing
3. Downloads and strips the HTML filing document
4. Sends the text to Groq (`llama-3.3-70b-versatile`) with a structured extraction prompt
5. Returns and logs the JSON brief

No database required. No paid data APIs. Just SEC EDGAR (free) and Claude.

## Architecture

See [CLAUDE.md](CLAUDE.md) for full architecture documentation, coding conventions, and the roadmap toward a full agent labor marketplace.
