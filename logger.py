"""
logger.py — Run logging to JSONL

Appends one JSON line per run to runs.jsonl. Each line captures the
ticker, timestamp, latency in seconds, and the full brief output.
This file is the audit trail for the marketplace — every agent invocation
is recorded so quality, latency, and signal accuracy can be tracked over time.
"""

import json
import os
import time
from datetime import datetime, timezone

RUNS_FILE = os.path.join(os.path.dirname(__file__), "runs.jsonl")


def log_run(ticker: str, brief: dict, latency_seconds: float) -> None:
    """Append a single run record to runs.jsonl."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker.upper(),
        "latency_seconds": round(latency_seconds, 3),
        "output": brief,
    }
    with open(RUNS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
