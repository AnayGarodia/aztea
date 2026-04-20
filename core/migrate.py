"""
migrate.py — SQLite schema migration runner for agentmarket.

Migrations are plain .sql files in the migrations/ directory at the repo root,
named with a numeric prefix: 0001_initial.sql, 0002_add_foo.sql, etc.

apply_migrations(db_path) is idempotent: already-applied migrations are
skipped.  Each migration runs in its own transaction so a partial failure
leaves the database in a consistent state.
"""

from __future__ import annotations

import os
import re
import sqlite3
import argparse

_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "migrations"
)
_MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_.+\.sql$")


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


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            filename    TEXT NOT NULL,
            applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def _is_idempotent_add_column_duplicate(statement: str, exc: sqlite3.OperationalError) -> bool:
    message = str(exc).strip().lower()
    if "duplicate column name" not in message:
        return False

    # Migration files can include comment lines before ALTER TABLE.
    # Strip comments so idempotency checks still detect ADD COLUMN statements.
    source = str(statement or "")
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    cleaned_lines: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        if "--" in line:
            line = line.split("--", 1)[0].strip()
        if line:
            cleaned_lines.append(line)
    normalized = " ".join(" ".join(cleaned_lines).strip().lower().split())
    return normalized.startswith("alter table ") and " add column " in normalized


def apply_migrations(db_path: str | None = None) -> list[int]:
    """
    Apply all pending migrations to the database at db_path.

    Returns the list of version numbers that were applied in this call.
    Already-applied migrations are skipped.
    """
    from core.db import DB_PATH as _DEFAULT_DB_PATH

    resolved_path = db_path or _DEFAULT_DB_PATH
    conn = sqlite3.connect(resolved_path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-64000")

    try:
        with conn:
            _ensure_migrations_table(conn)

        applied = _applied_versions(conn)
        newly_applied: list[int] = []

        for version, filepath in _migration_files():
            if version in applied:
                continue

            sql = open(filepath, encoding="utf-8").read()
            statements = [s.strip() for s in sql.split(";") if s.strip()]

            with conn:
                for statement in statements:
                    try:
                        conn.execute(statement)
                    except sqlite3.OperationalError as exc:
                        if _is_idempotent_add_column_duplicate(statement, exc):
                            continue
                        raise
                conn.execute(
                    "INSERT INTO schema_migrations (version, filename) VALUES (?, ?)",
                    (version, os.path.basename(filepath)),
                )
            newly_applied.append(version)

        return newly_applied
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply pending SQLite migrations.")
    parser.add_argument(
        "--db-path",
        dest="db_path",
        default=None,
        help="Optional database path override (defaults to DB_PATH env var / core.db default).",
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
