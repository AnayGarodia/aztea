"""
financial_cli.py — CLI entry point for the agentmarket financial research agent

Usage:
    python scripts/financial_cli.py AAPL
    python scripts/financial_cli.py MSFT

Prints a structured investment brief as JSON to stdout.
Logs the run to runs.jsonl for auditability.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from agents.financial.fetcher import get_filing_data
from agents.financial.synthesizer import synthesize_brief

_RUNS_FILE = os.path.join(os.path.dirname(__file__), "..", "runs.jsonl")


def _log_run(ticker: str, brief: dict, latency_seconds: float) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker.upper(),
        "latency_seconds": round(latency_seconds, 3),
        "output": brief,
    }
    with open(_RUNS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def run(ticker: str) -> dict:
    start = time.monotonic()
    filing_data = get_filing_data(ticker)
    brief = synthesize_brief(filing_data)
    latency = time.monotonic() - start
    _log_run(ticker, brief, latency)
    return brief


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/financial_cli.py <TICKER>", file=sys.stderr)
        sys.exit(1)

    ticker = sys.argv[1].strip().upper()
    if not ticker.isalpha() or len(ticker) > 5:
        print(f"Error: '{ticker}' does not look like a valid ticker symbol.", file=sys.stderr)
        sys.exit(1)

    try:
        brief = run(ticker)
        print(json.dumps(brief, indent=2))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
