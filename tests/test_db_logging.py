"""Regression tests for structured logging in core/db.py.

These tests pin the behaviour added when silent except blocks were replaced
with structured logging. They use pytest's caplog fixture to assert the right
log records fire, rather than relying on observable side effects (which the
underlying error handling intentionally hides from callers).
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from core import db


def test_dbconnection_exit_logs_rollback_failure(caplog):
    """A failing rollback during __exit__ must surface via _LOG.exception.

    Pre-cleanup this branch swallowed the rollback failure silently, hiding
    connection-state corruption. The exception logging must NOT mask the
    original exception that triggered __exit__.
    """

    class _BadConn:
        def commit(self):
            pass

        def rollback(self):
            raise sqlite3.OperationalError("simulated rollback failure")

    wrapped = db.DbConnection(_BadConn(), is_postgres=False)

    caplog.set_level(logging.ERROR, logger="core.db")
    with pytest.raises(RuntimeError, match="boom"):
        with wrapped:
            raise RuntimeError("boom")

    rollback_logs = [r for r in caplog.records if "Rollback failed" in r.getMessage()]
    assert rollback_logs, "expected rollback failure to be logged via _LOG.exception"
    assert any(r.levelno == logging.ERROR for r in rollback_logs)


def test_close_all_connections_logs_wal_checkpoint_failure(caplog, monkeypatch):
    """WAL checkpoint failure during shutdown must warn (not silently swallow)."""

    class _FakeRawConn:
        def __init__(self):
            self.closed = False

        def execute(self, sql):
            if "wal_checkpoint" in sql.lower():
                raise sqlite3.OperationalError("simulated checkpoint failure")
            return None

        def close(self):
            self.closed = True

    fake = _FakeRawConn()

    # Inject a fake tracked connection without going through real sqlite.
    with db._sqlite_open_connections_lock:
        db._sqlite_open_connections.append(fake)  # type: ignore[arg-type]

    # Force the SQLite branch even if the test environment somehow flips
    # IS_POSTGRES — the WAL checkpoint path is SQLite-only.
    monkeypatch.setattr(db, "IS_POSTGRES", False)

    caplog.set_level(logging.WARNING, logger="core.db")
    db.close_all_connections()

    assert fake.closed, "connection should still be closed even when checkpoint fails"
    checkpoint_logs = [
        r for r in caplog.records if "WAL checkpoint failed" in r.getMessage()
    ]
    assert checkpoint_logs, "expected WAL checkpoint failure to log a warning"
    assert all(r.levelno == logging.WARNING for r in checkpoint_logs)


def test_get_sqlite_connection_logs_dropped_closed_connection(caplog, tmp_path):
    """Reusing a thread-local connection that was closed out-of-band must log a debug breadcrumb and reopen cleanly."""
    if db.IS_POSTGRES:
        pytest.skip("SQLite-specific path")

    db_path = str(tmp_path / "drop_closed.db")

    # Open a connection and then close its raw handle to simulate the
    # close_all_connections-then-reuse path.
    first = db._get_sqlite_connection(db_path)
    first._conn.close()

    caplog.set_level(logging.DEBUG, logger="core.db")
    second = db._get_sqlite_connection(db_path)

    # New connection must be live.
    second.execute("SELECT 1").fetchone()

    drop_logs = [
        r
        for r in caplog.records
        if "dropping closed sqlite connection" in r.getMessage()
    ]
    assert drop_logs, "expected debug breadcrumb when dropping a closed sqlite connection"

    # Cleanup so other tests don't inherit the tmp_path connection.
    db.close_all_connections()


def test_get_sqlite_connection_reopens_when_db_path_changes(tmp_path):
    """Changing SQLite db_path must not reuse a thread-local connection to the old DB."""
    if db.IS_POSTGRES:
        pytest.skip("SQLite-specific path")

    first_path = str(tmp_path / "first.db")
    second_path = str(tmp_path / "second.db")

    first = db._get_sqlite_connection(first_path)
    first.execute("CREATE TABLE marker (value TEXT)")
    first.execute("INSERT INTO marker (value) VALUES (?)", ("first",))
    first.commit()

    second = db._get_sqlite_connection(second_path)
    second.execute("CREATE TABLE marker (value TEXT)")
    second.execute("INSERT INTO marker (value) VALUES (?)", ("second",))
    second.commit()

    row = second.execute("SELECT value FROM marker").fetchone()
    assert row["value"] == "second"
    assert getattr(second, "_db_path") != getattr(first, "_db_path")

    with sqlite3.connect(first_path) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "first"

    db.close_all_connections()
