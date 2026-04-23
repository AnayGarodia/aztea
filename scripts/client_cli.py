"""Thin compatibility CLI wrapper around the new `aztea` Python SDK."""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

try:
    from aztea import AzteaClient
    from aztea.errors import APIError, AzteaError
except ModuleNotFoundError:
    sdk_root = Path(__file__).resolve().parents[1] / "sdks" / "python"
    sys.path.insert(0, str(sdk_root))
    from aztea import AzteaClient
    from aztea.errors import APIError, AzteaError


def main() -> None:
    parser = argparse.ArgumentParser(description="Call Aztea endpoints via the Python SDK.")
    parser.add_argument("ticker", nargs="?", help="Stock ticker symbol (e.g. AAPL)")
    parser.add_argument("--host", default="http://localhost:8000", help="Server base URL")
    parser.add_argument("--registry-list", action="store_true", help="List registered agents.")
    parser.add_argument("--tag", default=None, help="Optional registry tag filter for --registry-list.")
    args = parser.parse_args()

    api_key = os.environ.get("API_KEY")
    if not api_key:
        print("Error: API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    if not args.registry_list and not args.ticker:
        parser.error("ticker is required unless --registry-list is provided.")

    client = AzteaClient(base_url=args.host, api_key=api_key)
    try:
        if args.registry_list:
            result = client.registry.list(tag=args.tag)
        else:
            result = client._request_json(  # compatibility path for existing examples
                "POST",
                "/analyze",
                json_body={"ticker": args.ticker},
            )
    except APIError as exc:
        print(f"Error {exc.status_code}: {exc.detail}", file=sys.stderr)
        sys.exit(1)
    except AzteaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
