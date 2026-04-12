"""
main.py — CLI entry point for the agentmarket financial research agent

Usage:
    python main.py AAPL
    python main.py MSFT

Prints a structured investment brief as JSON to stdout.
Logs the run to runs.jsonl for auditability.
"""

import json
import sys
import time

from dotenv import load_dotenv
load_dotenv()

from fetcher import get_filing_data
from synthesizer import synthesize_brief
from logger import log_run


def run(ticker: str) -> dict:
    """Fetch filing data, synthesize brief, log run, return brief dict."""
    start = time.monotonic()

    filing_data = get_filing_data(ticker)
    brief = synthesize_brief(filing_data)

    latency = time.monotonic() - start
    log_run(ticker, brief, latency)

    return brief


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python main.py <TICKER>", file=sys.stderr)
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
