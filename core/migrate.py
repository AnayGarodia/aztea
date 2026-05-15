"""
migrate.py — Dual-backend schema migration runner for aztea.

Migrations are plain .sql files in the migrations/ directory, named with a
numeric prefix: 0001_initial.sql, 0002_add_foo.sql, etc.

apply_migrations() is idempotent: already-applied migrations are skipped.
Each migration runs in its own transaction so a partial failure leaves the
database in a consistent state.

Backends:
  - PostgreSQL (DATABASE_URL starts with "postgresql://"): uses psycopg2.
  - SQLite (default): uses sqlite3 directly (not the thread-local pool,
    since this runs during bootstrap before the pool is needed).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import time
from datetime import datetime, timezone

from core import logging_utils

_LOG = logging.getLogger(__name__)

_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "migrations")
_MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_.+\.sql$")

# Detect backend at import time the same way core/db.py does.
_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_IS_POSTGRES: bool = _DATABASE_URL.startswith("postgresql://")

# Postgres advisory-lock identity used to serialise concurrent migration
# applies across uvicorn workers. The specific value is arbitrary; what
# matters is that it stays stable across deploys (so two workers booting
# the same release agree on which lock to take) and doesn't collide with
# other application-level advisory locks (today: none). Derived
# deterministically from the ASCII bytes of "aztea-mg" so the choice is
# self-documenting from the constant alone.
MIGRATION_ADVISORY_LOCK_ID: int = 4297493287

# Healthy migrations apply in milliseconds; even an unusually large schema
# change should fit comfortably under a minute. Time out at 60s so a
# stuck lock-holder fails the second worker fast (operator-visible) rather
# than wedging systemd indefinitely waiting on a hung deploy.
MIGRATION_LOCK_TIMEOUT_SECONDS: int = 60

# Polling interval for the non-blocking pg_try_advisory_lock loop. Short
# enough that the second worker picks up the lock the moment the first
# releases; long enough that two workers don't burn CPU spinning.
_MIGRATION_LOCK_POLL_INTERVAL_SECONDS: float = 0.5


def _migration_files() -> list[tuple[int, str]]:
    """Return sorted list of (sequence_number, full_path) for every migration file."""
    try:
        entries = os.listdir(_MIGRATIONS_DIR)
    except FileNotFoundError:
        return []

    result: list[tuple[int, str]] = []
    for name in entries:
        m = _MIGRATION_FILENAME_RE.match(name)
        if m:
            seq = int(m.group(1))
            result.append((seq, os.path.join(_MIGRATIONS_DIR, name)))
    result.sort(key=lambda t: t[0])
    return result


def _split_statements(sql: str) -> list[str]:
    """Split SQL on semicolons, skipping empty fragments and pure-comment lines.

    A fragment whose only non-blank lines are SQL comments (`-- ...`) is
    dropped — Postgres treats a comment-only buffer as an empty query and
    raises `psycopg2.ProgrammingError: can't execute an empty query`. This
    matters for documentary migrations like `SELECT 1;  -- no-op`, where
    the trailing comment becomes a phantom second statement after split.
    """
    out: list[str] = []
    for fragment in sql.split(";"):
        stripped = fragment.strip()
        if not stripped:
            continue
        non_comment_lines = [
            line for line in stripped.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        if not non_comment_lines:
            continue
        out.append(stripped)
    return out


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _ensure_migrations_table_sqlite(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            filename    TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )
    """)


def _applied_versions_sqlite(conn) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def _is_idempotent_add_column_duplicate(statement: str, exc: Exception) -> bool:
    message = str(exc).strip().lower()
    if "duplicate column name" not in message:
        return False
    source = str(statement or "")
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    cleaned: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        if "--" in line:
            line = line.split("--", 1)[0].strip()
        if line:
            cleaned.append(line)
    normalized = " ".join(" ".join(cleaned).strip().lower().split())
    return normalized.startswith("alter table ") and " add column " in normalized


def _apply_migrations_sqlite(db_path: str) -> list[int]:
    import sqlite3

    # Justified raw connection: bootstrap path — core/db.py pool depends on
    # the schema this function is about to apply.
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        with conn:
            _ensure_migrations_table_sqlite(conn)

        applied = _applied_versions_sqlite(conn)
        newly_applied: list[int] = []

        for version, filepath in _migration_files():
            if version in applied:
                continue

            sql = open(filepath, encoding="utf-8").read()
            statements = _split_statements(sql)

            with conn:
                for statement in statements:
                    try:
                        conn.execute(statement)
                    except sqlite3.OperationalError as exc:
                        if _is_idempotent_add_column_duplicate(statement, exc):
                            continue
                        raise
                conn.execute(
                    "INSERT INTO schema_migrations (version, filename, applied_at) VALUES (?, ?, ?)",
                    (
                        version,
                        os.path.basename(filepath),
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )
            newly_applied.append(version)

        return newly_applied
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PostgreSQL helpers
# ---------------------------------------------------------------------------

def _ensure_migrations_table_postgres(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                filename    TEXT NOT NULL,
                applied_at  TEXT NOT NULL
            )
        """)
    conn.commit()


def _applied_versions_postgres(conn) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        rows = cur.fetchall()
    return {row[0] for row in rows}


_SQLITE_ONLY_PATTERNS = (
    # SQLite autoincrement — PostgreSQL uses SERIAL / BIGSERIAL
    re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE),
    # SQLite type names
    re.compile(r"\bBLOB\b", re.IGNORECASE),
    # SQLite datetime functions
    re.compile(r"datetime\('now'\)", re.IGNORECASE),
    re.compile(r"strftime\([^)]+\)", re.IGNORECASE),
    # SQLite INSERT OR IGNORE (should already be converted, but keep as safety net)
    re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE),
)

_SQLITE_REPLACEMENTS = [
    # AUTOINCREMENT → BIGSERIAL pattern
    (
        re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE),
        "BIGSERIAL PRIMARY KEY",
    ),
    (re.compile(r"\bBLOB\b", re.IGNORECASE), "BYTEA"),
    (re.compile(r"datetime\('now'\)", re.IGNORECASE), "NOW()"),
    (re.compile(r"strftime\('%Y-%m-%dT%H:%M:%SZ',\s*'now'\)", re.IGNORECASE), "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"),
    (re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE), "INSERT"),
]

# Postgres doesn't support CREATE INDEX IF NOT EXISTS with a WHERE clause in
# older versions, but modern pg14+ does. Keep as-is and let postgres raise if
# it fails — the admin can review.


def _adapt_for_postgres(sql: str) -> str:
    """Convert SQLite-specific SQL syntax to PostgreSQL equivalents."""
    for pattern, replacement in _SQLITE_REPLACEMENTS:
        sql = pattern.sub(replacement, sql)
    return sql


def _is_idempotent_postgres(statement: str, exc: Exception) -> bool:
    """Return True if this is a benign 'already exists' error we can skip."""
    msg = str(exc).strip().lower()
    # psycopg2 wraps PG error codes; common idempotent cases:
    return any(phrase in msg for phrase in (
        "already exists",           # table/index/column already present
        "duplicate column",         # ALTER TABLE ADD COLUMN
        "column already exists",
    ))


@contextlib.contextmanager
def _postgres_migration_lock(conn):
    """Hold a session-level Postgres advisory lock for the migration window.

    Polls ``pg_try_advisory_lock`` every
    ``_MIGRATION_LOCK_POLL_INTERVAL_SECONDS`` until acquired or the
    ``MIGRATION_LOCK_TIMEOUT_SECONDS`` deadline trips. Session-level rather
    than transaction-level so a worker that crashes mid-migration releases
    the lock automatically when the connection drops — no manual cleanup
    required, and no risk of a wedged lock surviving a process death.

    The matching ``pg_advisory_unlock`` in the finally block is
    belt-and-suspenders: connection close already releases, but the
    explicit unlock keeps pool-reuse safe and makes failures observable
    in tests.
    """
    deadline = time.monotonic() + MIGRATION_LOCK_TIMEOUT_SECONDS
    wait_started = time.monotonic()
    acquired = False
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (MIGRATION_ADVISORY_LOCK_ID,),
            )
            acquired = bool(cur.fetchone()[0])
        conn.commit()
        if acquired:
            break
        if time.monotonic() >= deadline:
            logging_utils.log_event(
                _LOG,
                logging.ERROR,
                "migrations.lock.timeout",
                {
                    "lock_id": MIGRATION_ADVISORY_LOCK_ID,
                    "timeout_seconds": MIGRATION_LOCK_TIMEOUT_SECONDS,
                    "worker_pid": os.getpid(),
                },
            )
            raise RuntimeError(
                "Failed to acquire migration advisory lock "
                f"({MIGRATION_ADVISORY_LOCK_ID}) within "
                f"{MIGRATION_LOCK_TIMEOUT_SECONDS}s — another worker may "
                "be stuck applying migrations."
            )
        time.sleep(_MIGRATION_LOCK_POLL_INTERVAL_SECONDS)
    wait_seconds = round(time.monotonic() - wait_started, 3)
    logging_utils.log_event(
        _LOG,
        logging.INFO,
        "migrations.lock.acquired",
        {
            "lock_id": MIGRATION_ADVISORY_LOCK_ID,
            "worker_pid": os.getpid(),
            "wait_seconds": wait_seconds,
        },
    )
    try:
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (MIGRATION_ADVISORY_LOCK_ID,),
                )
            conn.commit()
        except Exception:
            # Connection close releases the lock too; log and move on.
            _LOG.debug(
                "advisory unlock failed; connection close will release",
                exc_info=True,
            )
        logging_utils.log_event(
            _LOG,
            logging.INFO,
            "migrations.lock.released",
            {
                "lock_id": MIGRATION_ADVISORY_LOCK_ID,
                "worker_pid": os.getpid(),
            },
        )


def _apply_pending_postgres_migrations(conn) -> list[int]:
    """Read pending migrations and apply each in its own transaction.

    Caller is responsible for holding the migration advisory lock so two
    workers don't race to insert the same ``schema_migrations`` row. With
    the lock held, a slow worker that arrives after another finished will
    see an empty pending list and iterate zero times.
    """
    _ensure_migrations_table_postgres(conn)
    applied = _applied_versions_postgres(conn)
    newly_applied: list[int] = []

    for version, filepath in _migration_files():
        if version in applied:
            continue

        sql = open(filepath, encoding="utf-8").read()
        adapted_sql = _adapt_for_postgres(sql)
        statements = _split_statements(adapted_sql)

        try:
            with conn:
                cur = conn.cursor()
                for statement in statements:
                    try:
                        cur.execute(statement)
                    except Exception as exc:
                        if _is_idempotent_postgres(statement, exc):
                            conn.rollback()
                            continue
                        raise
                cur.execute(
                    "INSERT INTO schema_migrations (version, filename, applied_at) VALUES (%s, %s, %s)",
                    (
                        version,
                        os.path.basename(filepath),
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )
        except Exception:
            conn.rollback()
            raise

        newly_applied.append(version)

    return newly_applied


def _apply_migrations_postgres(database_url: str) -> list[int]:
    import psycopg2

    conn = psycopg2.connect(database_url)
    conn.autocommit = False

    try:
        with _postgres_migration_lock(conn):
            return _apply_pending_postgres_migrations(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_migrations(db_path: str | None = None) -> list[int]:
    """Apply all pending migrations.

    In PostgreSQL mode, ``db_path`` is ignored — the DATABASE_URL env var is used.
    In SQLite mode, ``db_path`` defaults to the core.db default path.

    Returns the list of version numbers applied in this call.
    """
    if _IS_POSTGRES:
        return _apply_migrations_postgres(_DATABASE_URL)

    from core.db import DB_PATH as _DEFAULT_DB_PATH
    resolved_path = db_path or _DEFAULT_DB_PATH
    return _apply_migrations_sqlite(resolved_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply pending database migrations.")
    parser.add_argument(
        "--db-path",
        dest="db_path",
        default=None,
        help="SQLite database path override (ignored in PostgreSQL mode).",
    )
    args = parser.parse_args()
    applied = apply_migrations(args.db_path)
    if applied:
        print(f"Applied migrations: {', '.join(str(v) for v in applied)}")
    else:
        print("No pending migrations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
