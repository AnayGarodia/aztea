"""
client.py — Reference HTTP client for the agentmarket /analyze endpoint.

Demonstrates how another agent (or human) calls this agent over HTTP.
Reads API_KEY from .env so it works out of the box locally.

Usage:
    python client.py AAPL
    python client.py MSFT --host http://localhost:8000
"""
import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Call the agentmarket /analyze endpoint.")
    parser.add_argument("ticker", help="Stock ticker symbol (e.g. AAPL)")
    parser.add_argument("--host", default="http://localhost:8000", help="Server base URL")
    args = parser.parse_args()

    api_key = os.environ.get("API_KEY")
    if not api_key:
        print("Error: API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    try:
        resp = requests.post(
            f"{args.host}/analyze",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={"ticker": args.ticker},
            timeout=120,
        )
    except requests.RequestException as e:
        print(f"Network error: {e}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        print(f"Error {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(resp.json(), indent=2))


if __name__ == "__main__":
    main()
