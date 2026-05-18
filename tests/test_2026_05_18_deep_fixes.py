"""Regression tests for the 2026-05-18 deep-tier (D1–D5) fixes.

D1 — rolling-window latency
D2 — judge leader re-election (DB lease)
D3 — operator dispute response slot
D4 — receipt verification block
D5 — caller-side escrow for async jobs
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest


# ---------------------------------------------------------------------------
# Shared fixture — lazily creates a DB and runs FastAPI lifespan once.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fresh_db():
    """Module-scoped DB with all migrations applied via the FastAPI lifespan.

    Saves any env vars we mutate and restores them on teardown — without
    this, the AZTEA_DISPUTE_OPERATOR_RESPONSE_ENABLED flag leaks into
    later test modules (e.g. tests/test_disputes.py) and changes the
    initial dispute status from 'pending' to 'awaiting_operator'.
    """
    db_handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_handle.close()
    saved: dict[str, str | None] = {
        "API_KEY": os.environ.get("API_KEY"),
        "DB_PATH": os.environ.get("DB_PATH"),
        "AZTEA_DISPUTE_OPERATOR_RESPONSE_ENABLED":
            os.environ.get("AZTEA_DISPUTE_OPERATOR_RESPONSE_ENABLED"),
        "AZTEA_CALLER_ESCROW_ENABLED":
            os.environ.get("AZTEA_CALLER_ESCROW_ENABLED"),
    }
    os.environ.setdefault("API_KEY", "test-master-key")
    os.environ["DB_PATH"] = db_handle.name
    os.environ["AZTEA_DISPUTE_OPERATOR_RESPONSE_ENABLED"] = "1"
    os.environ["AZTEA_CALLER_ESCROW_ENABLED"] = "1"
    from fastapi.testclient import TestClient
    from server import app
    with TestClient(app):
        pass  # lifespan applies migrations + init
    try:
        yield db_handle.name
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            os.unlink(db_handle.name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# D1 — rolling-window latency.
# ---------------------------------------------------------------------------


def test_d1_avg_latency_reflects_ring_not_lifetime_running_avg(fresh_db):
    """avg_latency_ms now equals the mean of the last N ring samples.

    Pre-D1: a single 181 s sample took thousands of follow-ups to dilute.
    Post-D1: the avg is computed from the bounded ring so an old outlier
    naturally rolls off after the window fills.
    """
    from core.registry import agents_ops as _ops
    from core.registry.call_history import normalize_call_ring
    import json
    # 100 fast samples -> avg should be the mean of the 100 fast samples.
    samples = [50] * 99 + [200_000]  # 1 outlier + 99 fast
    ring_json = json.dumps(
        [{"latency_ms": s, "price_cents": 0} for s in samples],
        separators=(",", ":"),
    )
    avg = _ops._avg_latency_from_ring(ring_json)
    assert avg < 2500, f"new avg {avg} should be ≤2500 ms when 99/100 samples are fast"


# ---------------------------------------------------------------------------
# D2 — DB-based leader election.
# ---------------------------------------------------------------------------


def test_d2_lease_acquire_renew_release_takeover(fresh_db):
    from core import background_leases as bl
    # Worker A claims.
    assert bl.acquire_or_renew("test_kind", "workerA", lease_seconds=60) is True
    # Worker B can't take an active lease.
    assert bl.acquire_or_renew("test_kind", "workerB", lease_seconds=60) is False
    # Worker A renews.
    assert bl.acquire_or_renew("test_kind", "workerA", lease_seconds=60) is True
    # Holder snapshot.
    holder = bl.current_holder("test_kind")
    assert holder["holder_id"] == "workerA"
    # Release lets worker B take.
    assert bl.release("test_kind", "workerA") is True
    assert bl.acquire_or_renew("test_kind", "workerB", lease_seconds=60) is True
    bl.release("test_kind", "workerB")


def test_d2_dispute_judgments_have_unique_dispute_judge_index(fresh_db):
    """Migration 0054 added a UNIQUE INDEX preventing double-votes."""
    from core import db as _db
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='dispute_judgments' AND sql LIKE '%UNIQUE%'"
        ).fetchall()
    names = [r["name"] for r in rows]
    assert any("dispute_judge_uq" in n for n in names), names


# ---------------------------------------------------------------------------
# D3 — operator dispute response.
# ---------------------------------------------------------------------------


def test_d3_awaiting_operator_status_recognised(fresh_db):
    from core import disputes
    assert "awaiting_operator" in disputes.DISPUTE_STATUSES


def test_d3_feature_flag_default_off():
    """The slot is gated; default off until UI ships."""
    # Save + clear the flag so the module-level guard reflects defaults.
    saved = os.environ.pop("AZTEA_DISPUTE_OPERATOR_RESPONSE_ENABLED", None)
    try:
        from core import disputes
        assert disputes._operator_response_enabled() is False
    finally:
        if saved is not None:
            os.environ["AZTEA_DISPUTE_OPERATOR_RESPONSE_ENABLED"] = saved


def test_d3_disputes_table_has_operator_response_columns(fresh_db):
    """Migration 0055 added the three operator-response columns."""
    from core import db as _db
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        rows = conn.execute("PRAGMA table_info(disputes)").fetchall()
    columns = {row["name"] for row in rows}
    assert "operator_response_text" in columns
    assert "operator_response_at" in columns
    assert "operator_response_deadline" in columns


# ---------------------------------------------------------------------------
# D4 — receipt verification block.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,expected_verifier", [
    ("regex_tester", "self"),
    ("sbom_generator", "self"),
    ("jwt_validator", "self"),
    ("cve_lookup", "external"),
    ("github_releases", "external"),
    ("pypi_metadata", "external"),
    ("dependency_auditor", "external"),
    ("browser_agent", "external"),
])
def test_d4_verification_block_per_known_slug(slug, expected_verifier):
    from core import receipts
    from server.builtin_agents import constants as _consts
    # Try both underscore and hyphen forms — cve-lookup uses a hyphen
    # in its internal endpoint while most newer agents use underscores.
    candidates = {f"internal://{slug}", f"internal://{slug.replace('_', '-')}"}
    agent_id = None
    for aid, ep in _consts.BUILTIN_INTERNAL_ENDPOINTS.items():
        if ep in candidates:
            agent_id = aid
            break
    assert agent_id, f"no built-in agent with slug {slug}"
    block = receipts._verification_for_agent(agent_id)
    assert block["verifier"] == expected_verifier, (
        f"{slug} expected verifier={expected_verifier!r}, got {block}"
    )


def test_d4_unknown_agent_falls_back_to_unverified():
    from core import receipts
    block = receipts._verification_for_agent("00000000-0000-0000-0000-000000000000")
    assert block["verifier"] == "unverified"


# ---------------------------------------------------------------------------
# D5 — caller escrow lifecycle.
# ---------------------------------------------------------------------------


def test_d5_reserve_holds_funds_without_debiting(fresh_db):
    """Reserving funds bumps ``held_cents`` without modifying ``balance_cents``."""
    from core import db as _db, payments, jobs
    from core.payments import caller_escrow

    owner_id = f"user:d5-reserve-{uuid.uuid4().hex[:6]}"
    wallet = payments.get_or_create_wallet(owner_id)
    payments.deposit(wallet["wallet_id"], 1000, "seed")
    agent_wallet = payments.get_or_create_wallet(f"agent:d5-bot-{uuid.uuid4().hex[:6]}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    job = jobs.create_job(
        agent_id="agent-d5-1",
        caller_owner_id=owner_id,
        caller_wallet_id=wallet["wallet_id"],
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        price_cents=100,
        caller_charge_cents=100,
        charge_tx_id=f"dummy-{uuid.uuid4().hex[:8]}",
        agent_owner_id=f"user:operator-{uuid.uuid4().hex[:6]}",
        input_payload={},
    )
    # Capture the wallet state *immediately before* the reserve so the
    # assertion compares deltas instead of absolute values (other tests
    # in the suite may have already deposited into the same registry).
    before = payments.get_wallet(wallet["wallet_id"])
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        caller_escrow.reserve(
            conn, job_id=job["job_id"],
            caller_wallet_id=wallet["wallet_id"], amount_cents=150,
        )
        conn.commit()
    after = payments.get_wallet(wallet["wallet_id"])
    assert after["balance_cents"] == before["balance_cents"]
    assert after["held_cents"] == before["held_cents"] + 150


def test_d5_release_returns_held_funds(fresh_db):
    from core import db as _db, payments, jobs
    from core.payments import caller_escrow

    wallet = payments.get_or_create_wallet("user:d5-release")
    payments.deposit(wallet["wallet_id"], 500, "seed")
    agent_wallet = payments.get_or_create_wallet("agent:d5-bot-2")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    job = jobs.create_job(
        agent_id="agent-d5-2",
        caller_owner_id="user:d5-release",
        caller_wallet_id=wallet["wallet_id"],
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        price_cents=50,
        caller_charge_cents=50,
        charge_tx_id="dummy-2",
        agent_owner_id="user:operator-2",
        input_payload={},
    )
    before_held = payments.get_wallet(wallet["wallet_id"])["held_cents"]
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        caller_escrow.reserve(
            conn, job_id=job["job_id"],
            caller_wallet_id=wallet["wallet_id"], amount_cents=75,
        )
        conn.commit()
    assert payments.get_wallet(wallet["wallet_id"])["held_cents"] == before_held + 75
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        caller_escrow.release(conn, job_id=job["job_id"], note="test")
        conn.commit()
    assert payments.get_wallet(wallet["wallet_id"])["held_cents"] == before_held


def test_d5_feature_flag_default_off():
    saved = os.environ.pop("AZTEA_CALLER_ESCROW_ENABLED", None)
    try:
        from core.payments import caller_escrow
        assert caller_escrow.caller_escrow_enabled() is False
    finally:
        if saved is not None:
            os.environ["AZTEA_CALLER_ESCROW_ENABLED"] = saved
