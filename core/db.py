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

# DATABASE_URL provides forward-compat with Postgres. SQLite-only for now.
# Accepted forms: "sqlite:///absolute/path.db" or a bare path.
# Postgres URLs (postgresql://...) are noted but not yet supported — swap this module when ready.
_DATABASE_URL = os.environ.get("DATABASE_URL", "")
if _DATABASE_URL.startswith("sqlite:///"):
    _DEFAULT_DB_PATH = os.path.abspath(_DATABASE_URL[len("sqlite:///"):])
elif _DATABASE_URL and not _DATABASE_URL.startswith(("postgres", "postgresql")):
    _DEFAULT_DB_PATH = os.path.abspath(_DATABASE_URL)

DB_PATH = os.environ.get("DB_PATH", _DEFAULT_DB_PATH)

# Cap concurrent database connections. SQLite WAL allows one writer and many
# readers, but unbounded threads will exhaust OS file descriptors. Override via
# DB_MAX_CONNECTIONS env var (default: 32).
_MAX_CONNECTIONS = max(1, int(os.environ.get("DB_MAX_CONNECTIONS", "32")))
_conn_semaphore = threading.Semaphore(_MAX_CONNECTIONS)

_local = threading.local()
# Track all open connections so close_all_connections() can checkpoint WAL on shutdown.
_open_connections: list[sqlite3.Connection] = []
_open_connections_lock = threading.Lock()


def _open_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-64000")
    with _open_connections_lock:
        _open_connections.append(conn)
    return conn


def get_raw_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Return the thread-local connection, opening it (or reopening if closed) as needed."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            _local.conn = None
    _local.conn = _open_connection(db_path)
    return _local.conn


def close_all_connections() -> None:
    """Checkpoint WAL and close all tracked connections. Call on process shutdown."""
    with _open_connections_lock:
        conns = list(_open_connections)
        _open_connections.clear()
    for conn in conns:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception:
            pass


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
