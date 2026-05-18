"""Regression test for migration 0057's pre-existing-duplicate handling.

The first attempt to apply 0057 on prod (2026-05-18) crashed because
dispute_judgments had a real historical duplicate at
(865ca12d-..., llm_primary) — a re-vote that flipped the verdict
before PR #71's idempotency guard existed. CI passed because the
test fixtures never seeded duplicates. This test seeds the prod
scenario and asserts the migration applies cleanly.
"""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_DUP_DISPUTE_ID = "865ca12d-d963-45c5-b4d0-549ebf6b6db1"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Apply migrations through 0056 using the production migration
    runner (which knows how to no-op redundant ALTER TABLE statements
    that earlier migrations leave behind on SQLite). Then yield a raw
    connection so the test can simulate the prod state and apply 0057
    via executescript directly."""
    db_path = tmp_path / f"mig57-{uuid.uuid4().hex}.db"
    # Build a migrations directory containing only files 0001..0056 so
    # apply_migrations stops cleanly before the migration under test.
    isolated_mig = tmp_path / "migrations"
    isolated_mig.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if int(path.name[:4]) <= 56:
            shutil.copy(path, isolated_mig / path.name)
    from core import migrate as _migrate
    monkeypatch.setattr(_migrate, "_MIGRATIONS_DIR", isolated_mig)
    _migrate.apply_migrations(str(db_path))
    conn = sqlite3.connect(db_path)
    yield conn
    conn.close()


def _insert_judgment(
    conn: sqlite3.Connection,
    judgment_id: str,
    dispute_id: str,
    judge_kind: str,
    verdict: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO dispute_judgments "
        "  (judgment_id, dispute_id, judge_kind, verdict, reasoning, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (judgment_id, dispute_id, judge_kind, verdict, "test", created_at),
    )
    conn.commit()


def test_migration_0057_handles_pre_existing_duplicates(db):
    """Prod scenario: two llm_primary rows on one dispute, one secondary.

    Earlier row must survive (matches dispute resolution's first-vote-wins
    contract); UNIQUE INDEX must be created; duplicate inserts after the
    migration must fail.
    """
    _insert_judgment(db, "j-early", _DUP_DISPUTE_ID, "llm_primary",
                     "caller_wins", "2026-05-07T03:56:13+00:00")
    _insert_judgment(db, "j-late", _DUP_DISPUTE_ID, "llm_primary",
                     "agent_wins", "2026-05-08T16:43:06+00:00")
    _insert_judgment(db, "j-sec", _DUP_DISPUTE_ID, "llm_secondary",
                     "caller_wins", "2026-05-08T16:43:06.877+00:00")
    _insert_judgment(db, "j-clean", "d-untouched", "llm_primary",
                     "agent_wins", "2026-05-09T00:00:00+00:00")

    db.executescript((MIGRATIONS_DIR / "0057_background_worker_leases.sql").read_text())
    db.commit()

    primaries = db.execute(
        "SELECT judgment_id FROM dispute_judgments "
        "WHERE dispute_id = ? AND judge_kind = 'llm_primary'",
        (_DUP_DISPUTE_ID,),
    ).fetchall()
    assert primaries == [("j-early",)], (
        "Earliest llm_primary row should survive — matches the dispute's "
        "first-vote-wins resolution path."
    )

    untouched = db.execute(
        "SELECT judgment_id FROM dispute_judgments WHERE dispute_id = 'd-untouched'"
    ).fetchall()
    assert untouched == [("j-clean",)]

    with pytest.raises(sqlite3.IntegrityError):
        _insert_judgment(db, "j-new-dup", _DUP_DISPUTE_ID, "llm_primary",
                         "agent_wins", "2026-05-19T00:00:00+00:00")


def test_migration_0057_is_idempotent_on_clean_db(db):
    """Migration must apply cleanly when no duplicates exist (CI baseline)."""
    _insert_judgment(db, "j1", "d1", "llm_primary", "agent_wins",
                     "2026-05-10T00:00:00+00:00")
    _insert_judgment(db, "j2", "d2", "llm_primary", "caller_wins",
                     "2026-05-11T00:00:00+00:00")

    db.executescript((MIGRATIONS_DIR / "0057_background_worker_leases.sql").read_text())
    db.commit()

    rows = db.execute("SELECT COUNT(*) FROM dispute_judgments").fetchone()
    assert rows == (2,), "No-duplicate DB should be left untouched."

    indexes = {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='dispute_judgments'"
        )
    }
    assert "dispute_judgments_dispute_judge_uq" in indexes
