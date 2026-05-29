"""Tests for the hosted_execution_log audit writer.

Drives the public ``record_execution`` API through every code path:
happy insert, hash determinism, NULL-tolerant fields, kill-reason
recording, and the "never raise" contract under a broken DB.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def fresh_db(isolated_db, monkeypatch):
    """Reuse the integration isolated_db fixture so migration 0072 is
    applied. Also force core.db.DB_PATH at the module level so
    ``record_execution`` (which uses ``_db.get_db_connection()`` with
    no override) writes into the same isolated SQLite file the test
    reads from. The integration conftest monkeypatches DB_PATH on
    individual modules but NOT on core.db itself — explicit patch
    here keeps the test self-contained."""
    from core import db as _core_db
    monkeypatch.setattr(_core_db, "DB_PATH", str(isolated_db))
    return isolated_db


def _fetch_all_rows():
    from core import db as _db
    with _db.get_db_connection() as conn:
        cur = conn.execute(
            "SELECT execution_id, surface, skill_id, input_hash, output_hash, "
            "execution_time_ms, sandbox_exit_code, was_killed, kill_reason "
            "FROM hosted_execution_log ORDER BY created_at ASC"
        )
        return [dict(r) for r in cur.fetchall()]


def test_record_happy_path_inserts_row(fresh_db):
    from core.hosted_execution_log import record_execution
    exec_id = record_execution(
        surface="hosted_skill_call",
        execution_time_ms=42,
        sandbox_exit_code=0,
        input_payload={"x": 1},
        output_payload={"result": 2},
        caller_owner_id="user_abc",
        skill_id="skill_xyz",
    )
    assert exec_id is not None
    assert len(exec_id) == 32  # uuid4 hex

    rows = _fetch_all_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["surface"] == "hosted_skill_call"
    assert row["skill_id"] == "skill_xyz"
    assert row["execution_time_ms"] == 42
    assert row["sandbox_exit_code"] == 0
    assert row["was_killed"] == 0
    assert row["input_hash"] is not None
    assert row["output_hash"] is not None


def test_record_anonymous_playground_test_omits_caller(fresh_db):
    from core.hosted_execution_log import record_execution
    record_execution(
        surface="playground_test",
        execution_time_ms=120,
        sandbox_exit_code=0,
        input_payload="print(1)",
        output_payload="1\n",
    )
    row = _fetch_all_rows()[0]
    assert row["surface"] == "playground_test"
    assert row["skill_id"] is None


def test_hash_is_deterministic_for_identical_payload(fresh_db):
    from core.hosted_execution_log import record_execution
    record_execution(
        surface="playground_test",
        execution_time_ms=1,
        sandbox_exit_code=0,
        input_payload={"code": "x"},
    )
    record_execution(
        surface="playground_test",
        execution_time_ms=2,
        sandbox_exit_code=0,
        input_payload={"code": "x"},
    )
    rows = _fetch_all_rows()
    assert rows[0]["input_hash"] == rows[1]["input_hash"], (
        "Identical input_payload must hash to the same value (used for "
        "abuse-pattern correlation)."
    )


def test_hash_differs_for_different_payload(fresh_db):
    from core.hosted_execution_log import record_execution
    record_execution(
        surface="playground_test",
        execution_time_ms=1,
        sandbox_exit_code=0,
        input_payload={"code": "x"},
    )
    record_execution(
        surface="playground_test",
        execution_time_ms=1,
        sandbox_exit_code=0,
        input_payload={"code": "y"},
    )
    rows = _fetch_all_rows()
    assert rows[0]["input_hash"] != rows[1]["input_hash"]


def test_kill_reason_persisted(fresh_db):
    from core.hosted_execution_log import record_execution
    record_execution(
        surface="hosted_skill_call",
        execution_time_ms=2000,
        sandbox_exit_code=124,
        was_killed=True,
        kill_reason="timeout",
        skill_id="skill_runaway",
    )
    row = _fetch_all_rows()[0]
    assert row["was_killed"] == 1
    assert row["kill_reason"] == "timeout"
    assert row["sandbox_exit_code"] == 124


def test_record_never_raises_on_db_failure(monkeypatch, fresh_db):
    """The 'fire and forget' contract: any DB exception must be
    swallowed, the call must return None."""
    import core.hosted_execution_log as mod

    class _BrokenConn:
        def __enter__(self):
            raise RuntimeError("db on fire")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod._db, "get_db_connection", lambda: _BrokenConn())
    # Must NOT raise — the contract is no observability call ever
    # blocks the hot path.
    out = mod.record_execution(
        surface="playground_test", execution_time_ms=1, sandbox_exit_code=0,
    )
    assert out is None


def test_input_output_are_never_persisted_in_raw(fresh_db):
    """Direct assert on the schema: there is no input_payload or
    output_payload column — only hashes. This is a contract regression
    guard: if anyone adds a raw column in a future migration, this
    test fails loudly."""
    from core import db as _db
    with _db.get_db_connection() as conn:
        # SQLite-style introspection — works on both backends because
        # the column list is identical.
        cols = []
        try:
            cur = conn.execute("SELECT * FROM hosted_execution_log LIMIT 0")
            cols = [d[0] for d in cur.description]
        except Exception:
            pytest.skip("backend without column introspection")
    assert "input_payload" not in cols
    assert "output_payload" not in cols
    assert "input_hash" in cols
    assert "output_hash" in cols
