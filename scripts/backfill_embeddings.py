#!/usr/bin/env python3
"""Backfill missing agent embeddings in registry.db."""

from __future__ import annotations

import argparse
import json

from core import registry


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill missing agent embeddings.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of missing agents to embed in this run.",
    )
    args = parser.parse_args()

    registry.init_db()
    summary = registry.backfill_missing_embeddings(limit=args.limit)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
