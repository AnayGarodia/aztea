"""
db.py — Shared SQLite connection manager for all core modules.

Provides a thread-local connection pool with production-grade PRAGMAs applied
on every new connection. Use get_db_connection() as a context manager wherever
a SQLite connection is needed.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator

_DEFAULT_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "registry.db"))
DB_PATH = os.environ.get("DB_PATH", _DEFAULT_DB_PATH)

_local = threading.local()


def _open_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-64000")
    return conn


def get_raw_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Return the thread-local connection, opening it if not yet created."""
    if not getattr(_local, "conn", None):
        _local.conn = _open_connection(db_path)
    return _local.conn


@contextmanager
def get_db_connection(db_path: str = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager yielding the thread-local SQLite connection.

    Usage:
        with get_db_connection() as conn:
            conn.execute(...)

    The context manager does NOT open/close the connection on each call —
    the same thread-local connection is reused for efficiency.  Commits are
    handled by the sqlite3 connection's own context manager (``with conn``).
    """
    conn = get_raw_connection(db_path)
    yield conn
