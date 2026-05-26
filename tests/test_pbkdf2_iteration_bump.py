"""Regression tests for the PBKDF2 cost bump (100k → 600k) and the per-user
``pbkdf2_iterations`` column added by migration 0066.

Pin three behaviours:

  1. Legacy hashes (cost 100k) still verify after the constant rises.
  2. A successful login at the legacy cost rehashes the row at the new cost.
  3. New registrations write the current PBKDF2_ITERATIONS into the column.

These tests use an isolated SQLite db (the auth module's default backend in
tests) and exercise the public API from ``core.auth.users``.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import uuid

import pytest


@pytest.fixture
def auth_db(monkeypatch):
    """Spin up an isolated SQLite db with the auth schema applied."""
    fd, path = tempfile.mkstemp(suffix="-auth-pbkdf2.db")
    os.close(fd)

    # Point every auth helper at this file before it imports a connection.
    from core import db as _db
    monkeypatch.setattr(_db, "DB_PATH", path)
    monkeypatch.setattr(_db, "IS_POSTGRES", False)

    import core.auth as auth_pkg
    import core.auth.schema as schema_mod
    monkeypatch.setattr(auth_pkg, "DB_PATH", path)
    monkeypatch.setattr(schema_mod, "DB_PATH", path)

    # Drop any thread-local state from prior tests.
    if hasattr(_db._local, "conn"):
        try:
            _db._local.conn.close()
        except Exception:
            pass
        delattr(_db._local, "conn")

    # Apply the full migration set so the column shape matches prod.
    from core.migrate import apply_migrations
    apply_migrations(path)

    yield path

    # Close the thread-local connection so the file unlinks cleanly.
    if hasattr(_db._local, "conn"):
        try:
            _db._local.conn.close()
        except Exception:
            pass
        delattr(_db._local, "conn")
    try:
        os.unlink(path)
    except OSError:
        pass


def test_pbkdf2_iterations_constants_are_correct():
    """The constants are the contract — fail loudly if anyone downgrades."""
    from core.auth.schema import PBKDF2_ITERATIONS, PBKDF2_LEGACY_ITERATIONS

    assert PBKDF2_ITERATIONS == 600_000, "current default must match OWASP guidance"
    assert PBKDF2_LEGACY_ITERATIONS == 100_000, "legacy floor must match pre-bump constant"
    assert PBKDF2_LEGACY_ITERATIONS < PBKDF2_ITERATIONS, "legacy must be lower than current"


def test_hash_password_uses_supplied_cost():
    """``_hash_password`` must honour an explicit iteration count and produce
    different output for different costs even with identical salt+password."""
    from core.auth.schema import _hash_password

    salt = secrets.token_hex(32)
    h_100k = _hash_password("hunter2-test", salt, iterations=100_000)
    h_600k = _hash_password("hunter2-test", salt, iterations=600_000)
    assert h_100k != h_600k
    # Sanity: identical inputs reproduce the same output.
    assert h_100k == _hash_password("hunter2-test", salt, iterations=100_000)


def test_register_user_writes_current_iteration_count(auth_db):
    """Newly-registered users land at the current default cost."""
    from core import db as _db
    from core.auth.schema import PBKDF2_ITERATIONS
    from core.auth.users import register_user

    register_user(
        username="alice-pbk-test",
        email=f"alice-{uuid.uuid4().hex}@example.com",
        password="hunter2abc",
    )
    with _db.get_db_connection(auth_db) as conn:
        row = conn.execute(
            "SELECT pbkdf2_iterations FROM users WHERE username = %s",
            ("alice-pbk-test",),
        ).fetchone()
    assert row is not None
    assert int(row["pbkdf2_iterations"]) == PBKDF2_ITERATIONS


def test_login_verifies_legacy_hash_and_rehashes(auth_db):
    """A user whose row was written at the legacy cost should:
    (a) successfully verify their password,
    (b) be silently rehashed at the new cost on that same login.
    """
    from core import db as _db
    from core.auth.schema import (
        PBKDF2_ITERATIONS,
        PBKDF2_LEGACY_ITERATIONS,
        _hash_password,
    )
    from core.auth.users import login_user

    # Insert a synthetic legacy user row directly: hash at 100k, write
    # pbkdf2_iterations = 100000. Mimics any user who registered before the bump.
    email = f"legacy-{uuid.uuid4().hex}@example.com"
    user_id = str(uuid.uuid4())
    salt = secrets.token_hex(32)
    password = "legacy-secret-1"
    legacy_hash = _hash_password(password, salt, iterations=PBKDF2_LEGACY_ITERATIONS)
    with _db.get_db_connection(auth_db) as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, email, password_hash, salt, "
            "pbkdf2_iterations, created_at, role) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                user_id,
                "legacy-pbk-user",
                email,
                legacy_hash,
                salt,
                PBKDF2_LEGACY_ITERATIONS,
                "2024-01-01T00:00:00+00:00",
                "both",
            ),
        )

    # Login should succeed despite the cost mismatch with the current default.
    result = login_user(email=email, password=password)
    assert result is not None
    assert result["user_id"] == user_id

    # The row must have been rehashed at the new cost.
    with _db.get_db_connection(auth_db) as conn:
        row = conn.execute(
            "SELECT pbkdf2_iterations, password_hash, salt FROM users WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    assert int(row["pbkdf2_iterations"]) == PBKDF2_ITERATIONS
    assert row["password_hash"] != legacy_hash, "rehash should produce a new hash"
    assert row["salt"] != salt, "rehash must use a fresh salt"

    # And the rehashed row still verifies the original password.
    result2 = login_user(email=email, password=password)
    assert result2 is not None
    assert result2["user_id"] == user_id


def test_login_with_wrong_password_returns_none_for_legacy_row(auth_db):
    """Wrong-password verification must still fail closed on legacy rows."""
    from core import db as _db
    from core.auth.schema import PBKDF2_LEGACY_ITERATIONS, _hash_password
    from core.auth.users import login_user

    email = f"legacy-wrong-{uuid.uuid4().hex}@example.com"
    user_id = str(uuid.uuid4())
    salt = secrets.token_hex(32)
    legacy_hash = _hash_password(
        "right-password", salt, iterations=PBKDF2_LEGACY_ITERATIONS
    )
    with _db.get_db_connection(auth_db) as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, email, password_hash, salt, "
            "pbkdf2_iterations, created_at, role) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                user_id,
                "legacy-wrongpass",
                email,
                legacy_hash,
                salt,
                PBKDF2_LEGACY_ITERATIONS,
                "2024-01-01T00:00:00+00:00",
                "both",
            ),
        )
    assert login_user(email=email, password="wrong-password") is None
    # Row should NOT have been rehashed.
    with _db.get_db_connection(auth_db) as conn:
        row = conn.execute(
            "SELECT pbkdf2_iterations FROM users WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    assert int(row["pbkdf2_iterations"]) == PBKDF2_LEGACY_ITERATIONS
