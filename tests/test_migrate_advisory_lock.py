"""Conditional tests for the Postgres advisory-lock that serialises
``_apply_migrations_postgres`` across uvicorn workers.

Skipped on hosts without a Postgres ``DATABASE_URL`` — the race the lock
prevents only manifests under Postgres (SQLite already serialises via
``BEGIN IMMEDIATE``). Set ``DATABASE_URL=postgresql://...`` against a
disposable test database to enable the suite locally; CI hosts without
Postgres see four SKIPPED, not four MISSING. See PR #migration-advisory-lock
for the production incident this covers (2026-05-15 deploy of migration 0046).
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

_DATABASE_URL_RAW = os.environ.get("DATABASE_URL", "")
_POSTGRES = _DATABASE_URL_RAW.startswith("postgresql://")

pytestmark = pytest.mark.skipif(
    not _POSTGRES,
    reason="advisory-lock race tests require a Postgres DATABASE_URL",
)


@pytest.fixture
def isolated_pg(monkeypatch, tmp_path):
    """Point migrate at a per-test schema so concurrent test runs don't fight.

    Creates a temporary ``schema_migrations_<uuid>`` schema in the existing
    Postgres database, redirects the migration runner there for the test,
    and drops it on teardown. Avoids needing a separate test DB while
    still isolating the migration history table from neighbouring tests.
    """
    import psycopg2

    suffix = uuid.uuid4().hex[:8]
    schema_name = f"mig_lock_test_{suffix}"
    conn = psycopg2.connect(_DATABASE_URL_RAW)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema_name}"')
    conn.close()

    test_url = (
        f"{_DATABASE_URL_RAW}"
        + ("&" if "?" in _DATABASE_URL_RAW else "?")
        + f"options=-csearch_path%3D{schema_name}"
    )
    from core import migrate
    monkeypatch.setattr(migrate, "_DATABASE_URL", test_url)
    monkeypatch.setattr(migrate, "_IS_POSTGRES", True)

    yield {"url": test_url, "schema": schema_name}

    cleanup = psycopg2.connect(_DATABASE_URL_RAW)
    cleanup.autocommit = True
    with cleanup.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
    cleanup.close()


def _lock_held_by_anyone(database_url: str) -> bool:
    """Return True if some session holds MIGRATION_ADVISORY_LOCK_ID right now."""
    import psycopg2

    from core.migrate import MIGRATION_ADVISORY_LOCK_ID

    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_ID,)
            )
            acquired = bool(cur.fetchone()[0])
            if acquired:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_ID,)
                )
        conn.commit()
    finally:
        conn.close()
    return not acquired


def test_advisory_lock_serializes_two_workers(isolated_pg):
    """Two threads racing into ``_apply_migrations_postgres`` both succeed
    and the migration set is applied exactly once.
    """
    import psycopg2
    from core import migrate

    barrier = threading.Barrier(2)
    errors: list[Exception] = []
    results: list[list[int]] = []
    lock = threading.Lock()

    def _race():
        try:
            barrier.wait(timeout=10)
            applied = migrate._apply_migrations_postgres(isolated_pg["url"])
            with lock:
                results.append(applied)
        except Exception as exc:  # pragma: no cover — surfaced via assert
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, f"workers raised: {errors}"
    assert len(results) == 2

    # The first worker through the lock applies every pending migration;
    # the second sees them already in schema_migrations and applies none.
    applied_lengths = sorted(len(r) for r in results)
    assert applied_lengths[0] == 0, (
        f"second worker should be a no-op, got {applied_lengths[0]} applied"
    )
    assert applied_lengths[1] > 0, (
        "first worker should apply at least one migration"
    )

    # Independently confirm: every version exists exactly once in the table.
    conn = psycopg2.connect(isolated_pg["url"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT version, COUNT(*) AS n
                FROM schema_migrations
                GROUP BY version
                HAVING COUNT(*) > 1
                """
            )
            duplicates = cur.fetchall()
    finally:
        conn.close()
    assert duplicates == [], f"duplicate schema_migrations rows: {duplicates}"


def test_advisory_lock_released_on_success(isolated_pg):
    from core import migrate

    migrate._apply_migrations_postgres(isolated_pg["url"])
    # A fresh connection must be able to grab the lock immediately.
    assert not _lock_held_by_anyone(isolated_pg["url"]) is False  # noqa: SIM102
    assert _lock_held_by_anyone(isolated_pg["url"]) is False, (
        "advisory lock should be released after a successful migration run"
    )


def test_advisory_lock_released_on_exception(isolated_pg, monkeypatch):
    """If the migration step raises, the lock must still release so the
    next deploy attempt isn't permanently locked out.
    """
    from core import migrate

    def _boom(_conn):
        raise RuntimeError("forced failure inside the lock")

    monkeypatch.setattr(migrate, "_apply_pending_postgres_migrations", _boom)
    with pytest.raises(RuntimeError, match="forced failure inside the lock"):
        migrate._apply_migrations_postgres(isolated_pg["url"])
    assert _lock_held_by_anyone(isolated_pg["url"]) is False, (
        "advisory lock must release on exception so a retry can proceed"
    )


def test_advisory_lock_timeout_raises_runtime_error(isolated_pg, monkeypatch):
    """When another connection holds the lock past the deadline, the
    runner must raise RuntimeError rather than block indefinitely.
    """
    import psycopg2
    from core import migrate

    monkeypatch.setattr(migrate, "MIGRATION_LOCK_TIMEOUT_SECONDS", 2)

    holder = psycopg2.connect(isolated_pg["url"])
    try:
        with holder.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_lock(%s)",
                (migrate.MIGRATION_ADVISORY_LOCK_ID,),
            )
        holder.commit()
        started = time.monotonic()
        with pytest.raises(
            RuntimeError, match="Failed to acquire migration advisory lock"
        ):
            migrate._apply_migrations_postgres(isolated_pg["url"])
        elapsed = time.monotonic() - started
        # Must time out close to the budget — generous upper bound to
        # absorb test-host noise.
        assert elapsed < 10, (
            f"expected timeout near 2s, took {elapsed:.2f}s"
        )
    finally:
        with holder.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(%s)",
                (migrate.MIGRATION_ADVISORY_LOCK_ID,),
            )
        holder.commit()
        holder.close()
