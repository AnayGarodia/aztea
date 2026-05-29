"""C2 (2026-05-28): stability auto-flip tests.

Pure-logic tests don't need a DB. Integration tests use the isolated_db
fixture (tests/integration/conftest.py) so the 0067 migration runs and
real INSERTs hit the monitor's queries.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.registry import stability_monitor as sm


# --- Pure logic ---------------------------------------------------------


def test_is_endpoint_error_classifies_endpoint_markers():
    assert sm._is_endpoint_error("failed", "agent endpoint returned 503")
    assert sm._is_endpoint_error("failed", "connection refused")
    assert sm._is_endpoint_error("failed", "request timed out after 30s")
    assert sm._is_endpoint_error("cancelled", "no response from agent")


def test_is_endpoint_error_skips_input_validation_failures():
    # Input-validation failures are caller bugs, not agent illness.
    assert not sm._is_endpoint_error("failed", "missing required field: 'domain'")
    assert not sm._is_endpoint_error("failed", "invalid CVE id format")


def test_is_endpoint_error_skips_success_states():
    assert not sm._is_endpoint_error("complete", "")
    assert not sm._is_endpoint_error("complete", "endpoint returned 200")


def test_is_endpoint_error_skips_no_message_as_ambiguous():
    # /review M1 (2026-05-28): a failed job with no error_message is
    # ambiguous (pydantic ValidationError often serializes that way).
    # Don't count it as endpoint-side — better to miss a flip than
    # dark a legitimate agent for caller-side payload bugs.
    assert not sm._is_endpoint_error("failed", None)
    assert not sm._is_endpoint_error("failed", "")


def test_decide_flips_to_broken_above_threshold():
    stats = sm._AgentStats(
        agent_id="a1",
        total=50,
        errors=25,
        error_rate=0.50,
        last_n_streak_clean=False,
        current_override=None,
        distinct_error_callers=5,  # above Sybil-defense floor
    )
    d = sm._decide(stats, threshold=0.40)
    assert d is not None
    assert d.to_tier == "broken"
    assert d.from_tier is None
    assert "50%" in d.reason


def test_decide_does_not_flip_when_errors_all_from_one_caller():
    """/cso M4: Sybil-defense gate on distinct error callers."""
    stats = sm._AgentStats(
        agent_id="a-sybil",
        total=50,
        errors=25,
        error_rate=0.50,
        last_n_streak_clean=False,
        current_override=None,
        distinct_error_callers=1,  # all errors from one caller — self-flip attempt
    )
    assert sm._decide(stats, threshold=0.40) is None


def test_decide_skips_when_rate_below_threshold():
    stats = sm._AgentStats(
        agent_id="a1", total=50, errors=10, error_rate=0.20,
        last_n_streak_clean=False, current_override=None,
    )
    assert sm._decide(stats, threshold=0.40) is None


def test_decide_clears_override_on_recovery_streak():
    # Belt-and-suspenders M4: recovery now requires distinct callers
    # AND past the min-hold window. Use a last_flip_at well in the
    # past + 5 distinct recovery callers.
    from datetime import datetime, timedelta, timezone
    long_ago = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).isoformat()
    stats = sm._AgentStats(
        agent_id="a1", total=50, errors=2, error_rate=0.04,
        last_n_streak_clean=True, current_override="broken",
        distinct_recovery_callers=5,
        last_flip_at_iso=long_ago,
    )
    d = sm._decide(stats)
    assert d is not None
    assert d.from_tier == "broken"
    assert d.to_tier is None
    assert "recovery" in d.reason


def test_decide_holds_broken_during_min_hold_window():
    """Belt-and-suspenders M4: just-flipped agent stays broken even on clean streak."""
    from datetime import datetime, timezone
    just_now = datetime.now(timezone.utc).isoformat()
    stats = sm._AgentStats(
        agent_id="a-just-broken", total=50, errors=1, error_rate=0.02,
        last_n_streak_clean=True, current_override="broken",
        distinct_recovery_callers=10,
        last_flip_at_iso=just_now,
    )
    # Inside the min-hold window → no recovery.
    assert sm._decide(stats) is None


def test_decide_holds_broken_when_recovery_from_single_caller():
    """Belt-and-suspenders M4: self-clear by Sybil rejected."""
    from datetime import datetime, timedelta, timezone
    long_ago = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).isoformat()
    stats = sm._AgentStats(
        agent_id="a-self-clear", total=50, errors=1, error_rate=0.02,
        last_n_streak_clean=True, current_override="broken",
        distinct_recovery_callers=1,  # all clean calls from one caller
        last_flip_at_iso=long_ago,
    )
    assert sm._decide(stats) is None


def test_decide_keeps_broken_when_streak_not_clean():
    stats = sm._AgentStats(
        agent_id="a1", total=50, errors=5, error_rate=0.10,
        last_n_streak_clean=False, current_override="broken",
    )
    # Below flip threshold, but recovery streak isn't clean → stay broken.
    assert sm._decide(stats) is None


def test_decide_does_not_redo_flip_when_already_broken():
    stats = sm._AgentStats(
        agent_id="a1", total=50, errors=30, error_rate=0.60,
        last_n_streak_clean=False, current_override="broken",
    )
    # Already broken; no need to re-flip.
    assert sm._decide(stats) is None


# --- Integration --------------------------------------------------------


def _insert_agent(conn, *, agent_id: str, status: str = "active") -> None:
    """Bare-minimum INSERT for the agents table — only fields the
    monitor reads or the schema requires."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO agents (agent_id, owner_id, name, description, "
        "endpoint_url, price_per_call_usd, status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            agent_id, f"owner-{agent_id}", f"name-{agent_id}",
            "desc", "https://example.com/agent",
            0.05, status, now,
        ),
    )


import uuid as _job_id_uuid


def _insert_job(
    conn, *, agent_id: str, status: str,
    error_message: str | None = None,
    caller_owner_id: str = "caller-1",
) -> None:
    """Bare-minimum INSERT for the jobs table; only the columns the
    monitor reads + a few schema-required columns to satisfy CHECKs.

    The wallet / charge identifiers are placeholders — the monitor
    never looks at them; it reads status + error_message +
    agent_id + caller_owner_id (the last for Sybil defense).
    """
    job_id = f"job-{_job_id_uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO jobs ("
        "  job_id, agent_id, agent_owner_id, caller_owner_id, "
        "  caller_wallet_id, agent_wallet_id, platform_wallet_id, "
        "  status, price_cents, caller_charge_cents, charge_tx_id, "
        "  input_payload, error_message, created_at, updated_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            job_id, agent_id, f"owner-{agent_id}", caller_owner_id,
            "w-caller", "w-agent", "w-platform",
            status, 5, 5, "tx-1",
            "{}", error_message, now, now,
        ),
    )


@pytest.fixture
def isolated_db_for_monitor(monkeypatch, tmp_path):
    """Lightweight DB fixture — just runs migrations into a tmp file and
    monkeypatches DB_PATH everywhere the monitor and its callees look.

    Distinct from tests/integration/conftest.py::isolated_db so this
    test file can live under tests/ (not tests/integration/) and ship
    with the Phase 0.5 C2 commit.
    """
    import uuid as _uuid
    from pathlib import Path
    from core import db as _db
    from core.migrate import apply_migrations
    db_path = tmp_path / f"stability-{_uuid.uuid4().hex}.db"
    monkeypatch.setattr(_db, "DB_PATH", str(db_path))
    # Reset any thread-local connection cache so the new DB_PATH wins.
    if hasattr(_db._local, "conns"):
        for c in list(_db._local.conns.values()):
            try:
                c.close()
            except Exception:
                pass
        _db._local.conns.clear()
    apply_migrations(str(db_path))
    yield db_path


def test_integration_flips_to_broken_on_endpoint_error_burst(
    isolated_db_for_monitor,
):
    from core import db as _db
    agent_id = "agent-broken-1"
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        _insert_agent(conn, agent_id=agent_id)
        # 25 endpoint errors from 5 distinct callers (5 each)
        # — above the Sybil defense threshold (3).
        for i in range(25):
            _insert_job(
                conn, agent_id=agent_id, status="failed",
                error_message="endpoint returned 503",
                caller_owner_id=f"caller-{i % 5}",
            )
        for _ in range(25):
            _insert_job(conn, agent_id=agent_id, status="complete")
        conn.commit()

    result = sm.run_sweep()
    assert result.evaluated >= 1
    assert result.flipped_broken == 1

    with _db.get_raw_connection(_db.DB_PATH) as conn:
        row = conn.execute(
            "SELECT stability_override FROM agents WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
        assert row["stability_override"] == "broken"
        hist = conn.execute(
            "SELECT COUNT(*) AS n FROM stability_flip_history "
            "WHERE agent_id = %s", (agent_id,),
        ).fetchone()
        assert hist["n"] == 1


def test_integration_skips_suspended_agent(isolated_db_for_monitor):
    from core import db as _db
    agent_id = "agent-suspended-1"
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        _insert_agent(conn, agent_id=agent_id, status="suspended")
        for _ in range(40):
            _insert_job(conn, agent_id=agent_id, status="failed",
                        error_message="endpoint timeout")
        conn.commit()

    # Suspended agent is filtered out of _list_active_agent_ids, so
    # `evaluated` stays 0 and no flip ever happens.
    result = sm.run_sweep()
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        row = conn.execute(
            "SELECT stability_override FROM agents WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
        assert row["stability_override"] is None
    # No flip recorded.
    assert result.flipped_broken == 0


def test_integration_below_min_sample_does_nothing(isolated_db_for_monitor):
    from core import db as _db
    agent_id = "agent-fresh-1"
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        _insert_agent(conn, agent_id=agent_id)
        # Only 5 jobs total — below _DEFAULT_MIN_SAMPLE (10).
        for _ in range(5):
            _insert_job(conn, agent_id=agent_id, status="failed",
                        error_message="endpoint 502")
        conn.commit()

    result = sm.run_sweep()
    # Tiny sample window → monitor refuses to decide.
    assert result.flipped_broken == 0
