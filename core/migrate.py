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
import os
import re
from datetime import datetime, timezone

_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "migrations")
_MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_.+\.sql$")

# Detect backend at import time the same way core/db.py does.
_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_IS_POSTGRES: bool = _DATABASE_URL.startswith("postgresql://")


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


def _apply_migrations_postgres(database_url: str) -> list[int]:
    import psycopg2

    conn = psycopg2.connect(database_url)
    conn.autocommit = False

    try:
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
