"""
db.py — Dual-backend database connection manager.

Supports PostgreSQL (when DATABASE_URL starts with "postgresql://") and
SQLite (default, for tests and local dev). All callers should import from
this module instead of sqlite3 or psycopg2 directly.

# OWNS: connection pooling, backend selection, exception exports
# NOT OWNS: schema definitions, migrations (core/migrate.py), business logic
#
# INVARIANTS:
# - Never open raw sqlite3.connect() or psycopg2.connect() outside this module
# - All SQL uses %s placeholders; this module converts them to ? for SQLite
# - IS_POSTGRES is set at import time and never changes at runtime
# - IntegrityError / OperationalError / ProgrammingError are exported here
#   so callers never import from sqlite3 or psycopg2 directly
#
# DECISIONS:
# - Thread-local connections (one per thread) to avoid SQLite's check_same_thread
#   restriction and to bound psycopg2 connection count the same way
# - DbConnection wrapper normalises the cursor API so all callers get dict rows,
#   .rowcount, .lastrowid regardless of backend
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Generator, Iterable

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_DATABASE_URL = os.environ.get("DATABASE_URL", "")

# IS_POSTGRES is True when DATABASE_URL starts with "postgresql://"
IS_POSTGRES: bool = _DATABASE_URL.startswith("postgresql://")
DB_BACKEND: str = "postgres" if IS_POSTGRES else "sqlite"

# ---------------------------------------------------------------------------
# Exception exports — callers import from here, not from sqlite3/psycopg2
# ---------------------------------------------------------------------------

if IS_POSTGRES:
    try:
        import psycopg2
        import psycopg2.errors
        import psycopg2.extras

        IntegrityError = psycopg2.IntegrityError
        OperationalError = psycopg2.OperationalError
        ProgrammingError = psycopg2.ProgrammingError
    except ImportError as _pg_import_err:
        raise ImportError(
            "DATABASE_URL points to PostgreSQL but psycopg2 is not installed. "
            "Install it with: pip install psycopg2-binary"
        ) from _pg_import_err
else:
    IntegrityError = sqlite3.IntegrityError  # type: ignore[assignment,misc]
    OperationalError = sqlite3.OperationalError  # type: ignore[assignment,misc]
    ProgrammingError = sqlite3.ProgrammingError  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# SQLite path resolution (ignored when IS_POSTGRES)
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "registry.db")
)

if not IS_POSTGRES:
    if _DATABASE_URL.startswith("sqlite:///"):
        _DEFAULT_DB_PATH = os.path.abspath(_DATABASE_URL[len("sqlite:///"):])
    elif _DATABASE_URL and not _DATABASE_URL.startswith(("postgres", "postgresql")):
        _DEFAULT_DB_PATH = os.path.abspath(_DATABASE_URL)

DB_PATH = os.environ.get("DB_PATH", _DEFAULT_DB_PATH)

# Cap concurrent database connections to bound OS file descriptor usage.
# PostgreSQL backend uses the same semaphore for parity.
# Default raised 32→96 on 2026-05-08: 24-worker batches + sweeper + dispute
# judge + hook delivery + HTTP request handlers were exhausting the 32-conn
# pool when a 100+ job batch was in flight, stalling fan-out at ~87 settled.
_MAX_CONNECTIONS = max(1, int(os.environ.get("DB_MAX_CONNECTIONS", "96")))
_conn_semaphore = threading.BoundedSemaphore(_MAX_CONNECTIONS)

_local = threading.local()


# ---------------------------------------------------------------------------
# SQL placeholder normalisation
# ---------------------------------------------------------------------------

# Pre-compiled regex that replaces bare %s with ? for SQLite mode.
# Matches %s NOT preceded by another % (avoids %%s → %? mangling).
_PCT_S_RE = re.compile(r"(?<!%)%s")


def _to_sqlite_sql(sql: str) -> str:
    """Convert %s placeholders to ? for SQLite."""
    return _PCT_S_RE.sub("?", sql)


# ---------------------------------------------------------------------------
# DbConnection wrapper
# ---------------------------------------------------------------------------


class _CursorWrapper:
    """Wraps a DB-API 2 cursor and ensures fetchone/fetchall return dicts."""

    def __init__(self, cursor: Any, is_postgres: bool) -> None:
        self._cursor = cursor
        self._is_postgres = is_postgres

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        if self._is_postgres:
            # Postgres callers that need lastrowid should use RETURNING instead;
            # we return -1 as a sentinel rather than raising.
            return -1
        return self._cursor.lastrowid

    def fetchone(self) -> dict | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        if self._is_postgres:
            # psycopg2 RealDictCursor already returns dict-like; force plain dict.
            return dict(row)
        # sqlite3.Row supports dict conversion via dict() or key access.
        return dict(row)

    def fetchall(self) -> list[dict]:
        rows = self._cursor.fetchall()
        return [dict(r) for r in rows]


class DbConnection:
    """
    Thin wrapper over a raw DB-API 2 connection providing:
    - .execute(sql, params) — auto-converts %s → ? for SQLite; returns _CursorWrapper
    - .executemany(sql, params_list) — same placeholder conversion
    - Context manager: commit on __exit__ success, rollback on exception
    - .fetchone() / .fetchall() on the wrapper return plain dicts
    """

    def __init__(self, raw_conn: Any, is_postgres: bool) -> None:
        self._conn = raw_conn
        self._is_postgres = is_postgres

    # ------------------------------------------------------------------
    # Core execution helpers
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple | list | None = None) -> _CursorWrapper:
        effective_sql = sql if self._is_postgres else _to_sqlite_sql(sql)

        if self._is_postgres:
            # Translate SQLite-only statements so callers don't need IS_POSTGRES guards
            # for common pragma/transaction patterns.
            stripped = effective_sql.strip().upper()
            # BEGIN IMMEDIATE → BEGIN (psycopg2 is always in a transaction when autocommit=False)
            if stripped in ("BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"):
                effective_sql = "BEGIN"
            # PRAGMA statements have no Postgres equivalent; silently skip them.
            elif stripped.startswith("PRAGMA "):
                cursor = self._conn.cursor()
                return _CursorWrapper(cursor, self._is_postgres)

        if self._is_postgres:
            cursor = self._conn.cursor()
            cursor.execute(effective_sql, params)
        elif params is None:
            cursor = self._conn.execute(effective_sql)
        else:
            cursor = self._conn.execute(effective_sql, params)
        return _CursorWrapper(cursor, self._is_postgres)

    def executemany(
        self, sql: str, params_list: Iterable[tuple | list]
    ) -> _CursorWrapper:
        effective_sql = sql if self._is_postgres else _to_sqlite_sql(sql)
        if self._is_postgres:
            cursor = self._conn.cursor()
            cursor.executemany(effective_sql, params_list)
        else:
            cursor = self._conn.executemany(effective_sql, params_list)
        return _CursorWrapper(cursor, self._is_postgres)

    # ------------------------------------------------------------------
    # Transaction context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "DbConnection":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            try:
                self._conn.rollback()
            except Exception:
                # Swallow after logging: re-raising would mask the original
                # exception that triggered __exit__, which is the one callers
                # actually need to debug.
                _LOG.exception("Rollback failed during exception handling.")

    # ------------------------------------------------------------------
    # Passthrough helpers used by auth/schema init code
    # ------------------------------------------------------------------

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# SQLite backend — thread-local pool
# ---------------------------------------------------------------------------

# Track all open SQLite connections so close_all_connections() can checkpoint.
_sqlite_open_connections: list[sqlite3.Connection] = []
_sqlite_open_connections_lock = threading.Lock()


def _open_sqlite_connection(db_path: str) -> DbConnection:
    _conn_semaphore.acquire()
    try:
        raw = sqlite3.connect(db_path, check_same_thread=False, timeout=15)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA synchronous=NORMAL")
        raw.execute("PRAGMA busy_timeout=8000")
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("PRAGMA cache_size=-64000")
        raw.execute("PRAGMA wal_autocheckpoint=200")
    except Exception:
        _conn_semaphore.release()
        raise
    with _sqlite_open_connections_lock:
        _sqlite_open_connections.append(raw)
    return DbConnection(raw, is_postgres=False)


def _get_sqlite_connection(db_path: str) -> DbConnection:
    """Return the thread-local SQLite DbConnection, reopening if closed."""
    wrapper = getattr(_local, "conn", None)
    if wrapper is not None:
        try:
            wrapper._conn.execute("SELECT 1")
            return wrapper
        except sqlite3.ProgrammingError as exc:
            # Connection was closed out-of-band (e.g. close_all_connections()
            # ran on shutdown then a thread reused the wrapper). Drop it and
            # reopen — recoverable, but worth a debug breadcrumb.
            _LOG.debug("dropping closed sqlite connection from pool: %s", exc)
            with _sqlite_open_connections_lock:
                try:
                    _sqlite_open_connections.remove(wrapper._conn)
                except ValueError:
                    # Already removed by close_all_connections(); idempotent.
                    pass
            try:
                _conn_semaphore.release()
            except ValueError:
                # BoundedSemaphore.release() raises if released past initial
                # value; safe to ignore — semaphore was already balanced.
                pass
            _local.conn = None
    wrapper = _open_sqlite_connection(db_path)
    _local.conn = wrapper
    return wrapper


# ---------------------------------------------------------------------------
# PostgreSQL backend — thread-local connection
# ---------------------------------------------------------------------------


def _get_postgres_connection() -> DbConnection:
    """Return (or open) the thread-local psycopg2 DbConnection."""
    wrapper = getattr(_local, "conn", None)
    if wrapper is not None:
        try:
            # Lightweight liveness check — rollback any aborted transaction first.
            if wrapper._conn.closed:
                raise psycopg2.OperationalError("connection closed")
            wrapper._conn.rollback()
            wrapper._conn.cursor().execute("SELECT 1")
            return wrapper
        except Exception as exc:
            # Liveness check failed — recycle the connection. This is a
            # routine recoverable event (idle-disconnect, server restart),
            # so warn rather than exception. Operators watching logs need
            # to know recycling is happening if it spikes.
            _LOG.warning("postgres liveness check failed; recycling connection: %s", exc)
            _local.conn = None
            try:
                _conn_semaphore.release()
            except ValueError:
                # Idempotent release — see _get_sqlite_connection comment.
                pass

    _conn_semaphore.acquire()
    try:
        raw = psycopg2.connect(
            _DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        raw.autocommit = False
    except Exception:
        _conn_semaphore.release()
        raise
    wrapper = DbConnection(raw, is_postgres=True)
    _local.conn = wrapper
    return wrapper


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_raw_connection(db_path: str = DB_PATH) -> DbConnection:
    """Return the thread-local DbConnection (opening it if necessary).

    ``db_path`` is only used in SQLite mode. In PostgreSQL mode the connection
    string comes from ``DATABASE_URL`` and ``db_path`` is ignored.

    Only ``ProgrammingError`` (closed connection) triggers a reopen in SQLite
    mode. Other errors propagate so callers can handle them explicitly.
    """
    if IS_POSTGRES:
        return _get_postgres_connection()
    return _get_sqlite_connection(db_path)


@contextmanager
def get_db_connection(
    db_path: str = DB_PATH,
) -> Generator[DbConnection, None, None]:
    """Context manager yielding the thread-local DbConnection.

    Usage:
        with get_db_connection() as conn:
            conn.execute(...)

    The connection is NOT opened/closed on each call — the same thread-local
    connection is reused. Transaction management (commit/rollback) is handled
    by ``with conn:`` blocks inside the callers.
    """
    yield get_raw_connection(db_path)


def close_all_connections() -> None:
    """Close all tracked connections. Call on process shutdown."""
    if IS_POSTGRES:
        # PostgreSQL: close the thread-local connection if present.
        wrapper = getattr(_local, "conn", None)
        if wrapper is not None:
            try:
                wrapper.close()
            except Exception:
                _LOG.exception("Failed to close PostgreSQL connection during shutdown.")
            try:
                _conn_semaphore.release()
            except ValueError:
                # Idempotent release — see _get_sqlite_connection comment.
                pass
            _local.conn = None
        return

    # SQLite: checkpoint WAL and close all tracked connections.
    with _sqlite_open_connections_lock:
        conns = list(_sqlite_open_connections)
        _sqlite_open_connections.clear()
    for raw in conns:
        try:
            raw.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as exc:
            # Best-effort during shutdown — checkpoint failure leaves the
            # WAL on disk but the next process startup will replay it.
            # Don't re-raise; shutdown must continue closing connections.
            _LOG.warning("sqlite WAL checkpoint failed during shutdown: %s", exc)
        try:
            raw.close()
        except Exception:
            _LOG.exception("Failed to close SQLite connection during shutdown.")
        try:
            _conn_semaphore.release()
        except ValueError:
            # Idempotent release — see _get_sqlite_connection comment.
            pass
