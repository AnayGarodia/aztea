"""
Lightweight observability helpers for Aztea.

Provides:
  - timed()      context manager that records wall-clock milliseconds
  - record_call  persists a row to tool_invocation_metrics (best-effort)

Designed to add zero overhead when the metrics table isn't available yet
(i.e., before migration 0028 runs) and to never raise.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import Generator

from core import db as _db

logger = logging.getLogger(__name__)


class CallTimer:
    """Collects timing data accumulated inside a `timed()` block."""

    def __init__(self) -> None:
        self.elapsed_ms: float = 0.0

    def __repr__(self) -> str:  # noqa: D105
        return f"<CallTimer {self.elapsed_ms:.1f} ms>"


@contextmanager
def timed() -> Generator[CallTimer, None, None]:
    """Context manager that measures wall-clock time in milliseconds.

    Usage::

        with timed() as t:
            result = do_work()
        print(t.elapsed_ms)
    """
    timer = CallTimer()
    start = time.perf_counter()
    try:
        yield timer
    finally:
        timer.elapsed_ms = (time.perf_counter() - start) * 1000.0


def record_call(
    *,
    agent_id: str,
    caller_id: str | None,
    latency_ms: float,
    bytes_in: int = 0,
    bytes_out: int = 0,
    cached: bool = False,
) -> None:
    """Persist a call metric row.  Never raises — failures are logged only.

    Silently skips if the metrics table doesn't exist yet (pre-migration).
    """
    try:
        conn: sqlite3.Connection = _db.get_raw_connection(_db.DB_PATH)
        conn.execute(
            """
            INSERT INTO tool_invocation_metrics
                (agent_id, caller_id, latency_ms, bytes_in, bytes_out, cached, created_at)
            VALUES
                (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (agent_id, caller_id, latency_ms, bytes_in, bytes_out, int(cached)),
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        # Table missing pre-migration — ignore.
        if "no such table" not in str(exc):
            logger.debug("observability: metric write failed: %s", exc)
    except Exception as exc:  # pragma: no cover
        logger.debug("observability: metric write failed: %s", exc)
