"""
Regression tests — one test per bug fix.

Fix 1: _caller_from_raw_api_key used wrong auth function name
Fix 2: MCP manifest used camelCase keys and aztea__ prefix
Fix 3: get_agents() did not filter suspended agents
Fix 4: TrustGauge used raw success_rate instead of backend trust_score (frontend)
Fix 5: ApiKeyRow copied key_prefix instead of warning user (frontend)
Fix 6: Legacy unused components still present on disk
Fix 7: disputes.py duplicated the caller_ratings table definition
Fix 8: GET /runs lacked X-Skipped-Lines header for decode failures
Fix 9: post_call_refund_difference refunded caller without clawing back
       from agent/platform, creating phantom balance on every overestimate
"""

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fix 1 — verify_agent_api_key is called, not the non-existent verify_agent_key
# ---------------------------------------------------------------------------

def test_fix1_auth_module_exposes_verify_agent_api_key_not_verify_agent_key():
    """The correct function name must exist; the old wrong name must not."""
    from core import auth
    assert callable(getattr(auth, "verify_agent_api_key", None)), (
        "auth.verify_agent_api_key must exist"
    )
    assert not hasattr(auth, "verify_agent_key"), (
        "auth.verify_agent_key is the old wrong name and must not exist"
    )


def test_fix1_server_calls_verify_agent_api_key(tmp_path):
    """The _caller_from_raw_api_key function in server.py must call verify_agent_api_key."""
    import inspect
    import server.application as server
    src = inspect.getsource(server._caller_from_raw_api_key)
    assert "verify_agent_api_key" in src, (
        "_caller_from_raw_api_key must call verify_agent_api_key"
    )
    assert "verify_agent_key(" not in src, (
        "_caller_from_raw_api_key must not call the old verify_agent_key"
    )


# ---------------------------------------------------------------------------
# Fix 2 — MCP manifest keys are snake_case and tool names have no prefix
# ---------------------------------------------------------------------------

def test_fix2_mcp_manifest_uses_snake_case_keys():
    from core import mcp_manifest
    agents = [
        {
            "agent_id": "aaaaaaaa-0000-0000-0000-000000000001",
            "name": "Test Agent",
            "description": "A test.",
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {"type": "object", "properties": {}},
        }
    ]
    entries = mcp_manifest.build_mcp_tool_entries(agents)
    tool = entries[0]["tool"]
    assert "input_schema" in tool,  "key must be input_schema (snake_case)"
    assert "output_schema" in tool, "key must be output_schema (snake_case)"
    assert "inputSchema" not in tool,  "camelCase inputSchema must not be present"
    assert "outputSchema" not in tool, "camelCase outputSchema must not be present"


def test_fix2_mcp_tool_names_have_no_prefix():
    from core import mcp_manifest
    agents = [
        {
            "agent_id": "aaaaaaaa-0000-0000-0000-000000000002",
            "name": "My Agent",
            "description": "desc",
            "input_schema": {},
            "output_schema": {},
        }
    ]
    entries = mcp_manifest.build_mcp_tool_entries(agents)
    name = entries[0]["tool_name"]
    assert not name.startswith("aztea__"), (
        f"tool name '{name}' must not have aztea__ prefix"
    )
    assert name == "my_agent", f"expected 'my_agent', got '{name}'"


# ---------------------------------------------------------------------------
# Fix 3 — get_agents() excludes suspended agents (not just banned)
# ---------------------------------------------------------------------------

@pytest.fixture()
def registry_db(tmp_path, monkeypatch):
    from core import registry, reputation, auth, payments, jobs, disputes
    db_path = str(tmp_path / "reg.db")

    def _close(module):
        conn = getattr(getattr(module, "_local", None), "conn", None)
        if conn:
            conn.close()
            try:
                delattr(module._local, "conn")
            except AttributeError:
                pass

    modules = (registry, reputation, auth, payments, jobs, disputes)
    for m in modules:
        _close(m)
        monkeypatch.setattr(m, "DB_PATH", db_path)

    # Stub out embeddings so registration doesn't need the model
    dim = registry.embeddings.EMBEDDING_DIM
    monkeypatch.setattr(registry.embeddings, "embed_text", lambda _: [0.0] * dim)

    registry.init_db()
    reputation.init_reputation_db()
    yield db_path

    for m in modules:
        _close(m)


def test_fix3_get_agents_excludes_suspended(registry_db, monkeypatch):
    from core import registry
    active_id = registry.register_agent(
        name="Active Agent", description="active", endpoint_url="https://example.com/a",
        price_per_call_usd=0.01, tags=[],
    )
    suspended_id = registry.register_agent(
        name="Suspended Agent", description="suspended", endpoint_url="https://example.com/s",
        price_per_call_usd=0.01, tags=[],
    )
    registry.set_agent_status(suspended_id, "suspended")

    agents = registry.get_agents()
    ids = {a["agent_id"] for a in agents}
    assert active_id in ids, "active agent should appear"
    assert suspended_id not in ids, "suspended agent must be excluded"


def test_fix3_get_agents_excludes_banned(registry_db, monkeypatch):
    from core import registry
    banned_id = registry.register_agent(
        name="Banned Agent", description="banned", endpoint_url="https://example.com/b",
        price_per_call_usd=0.01, tags=[],
    )
    registry.set_agent_status(banned_id, "banned")

    agents = registry.get_agents()
    ids = {a["agent_id"] for a in agents}
    assert banned_id not in ids, "banned agent must be excluded"


# ---------------------------------------------------------------------------
# Fix 4 & Fix 5 — frontend JSX changes; verified via source inspection
# ---------------------------------------------------------------------------

def test_fix4_trust_gauge_uses_trust_score_field():
    """TrustGauge.jsx must reference agent.trust_score, not agent.success_rate."""
    jsx = Path(__file__).resolve().parent.parent / "frontend/src/features/agents/TrustGauge.jsx"
    src = jsx.read_text()
    assert "trust_score" in src, "TrustGauge must use trust_score"
    assert "success_rate" not in src, "TrustGauge must not use raw success_rate"


def test_fix5_keys_page_does_not_copy_prefix_silently():
    """KeysPage must not silently copy the prefix to the clipboard.

    The "Only the prefix is stored" note was intentionally removed in the
    Settings revamp — the dedicated Keys page already shows the prefix in
    monospace and labels it, which makes the storage model self-evident.
    """
    jsx = Path(__file__).resolve().parent.parent / "frontend/src/pages/KeysPage.jsx"
    src = jsx.read_text()
    # The old buggy handleCopy wrote the prefix+ellipsis to the clipboard
    assert "key_prefix + '…'" not in src, (
        "ApiKeyRow must not copy the key prefix to the clipboard"
    )


# ---------------------------------------------------------------------------
# Fix 6 — legacy unused components must be deleted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", [
    "Dashboard.jsx",
    "CallWorkspace.jsx",
    "ActivityPanel.jsx",
    "RegisterAgentModal.jsx",
    "LandingPage.jsx",
])
def test_fix6_legacy_components_deleted(filename):
    path = (
        Path(__file__).resolve().parent.parent
        / "frontend/src/components"
        / filename
    )
    assert not path.exists(), (
        f"{filename} is a legacy unused component and must be deleted"
    )


# ---------------------------------------------------------------------------
# Fix 7 — disputes.py must NOT define the caller_ratings table
# ---------------------------------------------------------------------------

def test_fix7_disputes_does_not_define_caller_ratings_table():
    """disputes.py must not contain a CREATE TABLE … caller_ratings block."""
    src = (Path(__file__).resolve().parent.parent / "core/disputes.py").read_text()
    # Allow only comments/mentions; reject the DDL
    assert "CREATE TABLE IF NOT EXISTS caller_ratings" not in src, (
        "caller_ratings must only be defined in reputation.py"
    )


def test_fix7_caller_ratings_defined_in_reputation():
    src = (Path(__file__).resolve().parent.parent / "core/reputation.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS caller_ratings" in src, (
        "caller_ratings canonical definition must remain in reputation.py"
    )


# ---------------------------------------------------------------------------
# Fix 8 — GET /runs returns X-Skipped-Lines header for malformed JSON lines
# ---------------------------------------------------------------------------

def test_fix8_runs_endpoint_emits_skipped_lines_header(tmp_path, monkeypatch):
    """When runs.jsonl contains invalid JSON lines the header must count them."""
    import server.application as server
    from fastapi.testclient import TestClient

    runs_file = tmp_path / "runs.jsonl"
    runs_file.write_text(
        '{"id":"r1","status":"ok"}\n'
        'not-valid-json\n'
        '{"id":"r2","status":"ok"}\n'
        'also bad\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(server, "_REPO_ROOT", str(tmp_path))

    master_key = "test-skip-header-key"
    monkeypatch.setattr(server, "_MASTER_KEY", master_key)

    client = TestClient(server.app, raise_server_exceptions=False)
    resp = client.get(
        "/runs",
        headers={"Authorization": f"Bearer {master_key}"},
    )
    assert resp.status_code == 200
    assert "x-skipped-lines" in {k.lower() for k in resp.headers}
    assert resp.headers.get("x-skipped-lines") == "2", (
        f"expected 2 skipped lines, got {resp.headers.get('x-skipped-lines')}"
    )
    body = resp.json()
    assert body["skipped_lines"] == 2
    assert body["skipped_line_numbers"] == [2, 4]


# ---------------------------------------------------------------------------
# Fix 9 — variable-pricing refund must stay zero-sum across caller/agent/platform
# ---------------------------------------------------------------------------

def test_fix9_refund_difference_claws_back_from_agent_and_platform(tmp_path, monkeypatch):
    """A variable-pricing refund must reverse the proportional agent + platform
    payout, not just credit the caller — otherwise every overestimate would
    create phantom balance that no external deposit funds.
    """
    import sys
    import uuid as _uuid
    import sqlite3 as _sqlite3

    db_path = tmp_path / f"test-ledger-{_uuid.uuid4().hex}.db"
    from core import db as _db
    from core import payments

    # Clear any thread-local connection the modules may have cached in
    # earlier tests so they reopen against our isolated DB.
    for module in (_db, payments):
        conn = getattr(module._local, "conn", None)
        if conn is not None:
            conn.close()
            try:
                delattr(module._local, "conn")
            except AttributeError:
                pass

    monkeypatch.setattr(_db, "DB_PATH", str(db_path))
    monkeypatch.setattr(payments, "DB_PATH", str(db_path))
    pkg = sys.modules.get("core.payments")
    if pkg is not None:
        monkeypatch.setattr(pkg, "DB_PATH", str(db_path), raising=False)
    # Apply schema.
    with _sqlite3.connect(str(db_path)) as _bootstrap:
        _bootstrap.execute("PRAGMA journal_mode=WAL")
    payments.init_payments_db()

    caller_wallet   = payments.get_or_create_wallet("user:caller")
    agent_wallet    = payments.get_or_create_wallet("agent:test-agent")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    # Fund the caller with 500¢ so we can debit 110¢ and track drift.
    payments.deposit(caller_wallet["wallet_id"], 500, memo="test bootstrap")

    # Pre-charge 100¢ + 10¢ platform fee (caller-bearer policy).
    distribution = payments.compute_success_distribution(
        100, platform_fee_pct=10, fee_bearer_policy="caller"
    )
    caller_charge_cents = int(distribution["caller_charge_cents"])
    assert caller_charge_cents == 110

    charge_tx_id = _direct_charge(
        payments, caller_wallet["wallet_id"], caller_charge_cents, "test-agent"
    )

    # Settle the "successful" job: agent gets 90, platform gets 10.
    payments.post_call_payout(
        agent_wallet["wallet_id"],
        platform_wallet["wallet_id"],
        charge_tx_id,
        100,
        "test-agent",
        platform_fee_pct=10,
        fee_bearer_policy="caller",
    )

    # Snapshot balances after forward path: caller -110, agent +100, platform +10.
    # Under caller-bearer fee policy, the fee rides on top of the price, so
    # agent keeps the full price and platform gets the fee.
    assert _wallet_balance(payments, caller_wallet["wallet_id"])   == 500 - 110
    assert _wallet_balance(payments, agent_wallet["wallet_id"])    == 100
    assert _wallet_balance(payments, platform_wallet["wallet_id"]) == 10

    # The agent reports half the work was done — actual price_cents=50.
    # Correct forward settlement for $0.50 under the same fee policy:
    #   caller_charge = 55, agent_payout = 50, platform_fee = 5
    # So refund must be: caller +55, agent -50, platform -5. Zero-sum.
    refund_tx_id = payments.post_call_refund_difference(
        caller_wallet["wallet_id"],
        charge_tx_id,
        55,
        "test-agent",
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        agent_clawback_cents=50,
        platform_clawback_cents=5,
        memo="half-usage",
    )
    assert refund_tx_id is not None

    # The zero-sum invariant: every transaction related to this charge,
    # together with the charge itself, must sum to zero. If the old code
    # ran (refund caller without clawback), the sum would be +55.
    related_sum = _related_tx_sum(payments, charge_tx_id)
    assert related_sum == 0, (
        f"related_tx sum must be 0 for a zero-sum refund, got {related_sum}. "
        "Caller refund was applied without corresponding agent/platform "
        "clawback — this is the phantom-balance bug."
    )

    # Individual wallet balances now reflect the refund.
    assert _wallet_balance(payments, caller_wallet["wallet_id"])   == 500 - 110 + 55
    assert _wallet_balance(payments, agent_wallet["wallet_id"])    == 100 - 50
    assert _wallet_balance(payments, platform_wallet["wallet_id"]) == 10 - 5

    # Total wallet balance must equal the original external deposit (500),
    # independent of how many internal transfers happened.
    total_balance = _total_wallet_balance(payments)
    assert total_balance == 500, (
        f"total wallet balance drifted to {total_balance} — variable pricing "
        "must not create or destroy money."
    )

    # A second refund attempt for the same charge should be a no-op (idempotent).
    second_tx_id = payments.post_call_refund_difference(
        caller_wallet["wallet_id"],
        charge_tx_id,
        55,
        "test-agent",
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        agent_clawback_cents=50,
        platform_clawback_cents=5,
        memo="half-usage-retry",
    )
    assert second_tx_id is None
    assert _total_wallet_balance(payments) == 500
    assert _related_tx_sum(payments, charge_tx_id) == 0

    # And the non-zero-sum guard: passing a bad split must raise — we
    # never want a caller to smuggle in a refund that creates cents.
    with pytest.raises(ValueError):
        payments.post_call_refund_difference(
            caller_wallet["wallet_id"],
            charge_tx_id,
            55,
            "test-agent",
            agent_wallet_id=agent_wallet["wallet_id"],
            platform_wallet_id=platform_wallet["wallet_id"],
            agent_clawback_cents=10,  # does not net to 55
            platform_clawback_cents=5,
        )


def _direct_charge(payments_mod, caller_wallet_id: str, amount_cents: int, agent_id: str) -> str:
    """Bypass pre_call_charge's API-key metering — we only need a charge row."""
    import sqlite3
    # Use the module's own connection path so the tx is visible to helpers.
    with sqlite3.connect(payments_mod._resolved_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        # Insert the charge directly so we don't need the auth plumbing.
        import uuid as _uuid
        tx_id = str(_uuid.uuid4())
        conn.execute(
            """
            INSERT INTO transactions
                (tx_id, wallet_id, type, amount_cents, related_tx_id,
                 agent_id, charged_by_key_id, memo, created_at)
            VALUES (?, ?, 'charge', ?, NULL, ?, NULL, 'test charge', datetime('now'))
            """,
            (tx_id, caller_wallet_id, -int(amount_cents), agent_id),
        )
        conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents - ? WHERE wallet_id = ?",
            (int(amount_cents), caller_wallet_id),
        )
        conn.commit()
    return tx_id


def _wallet_balance(payments_mod, wallet_id: str) -> int:
    import sqlite3
    with sqlite3.connect(payments_mod._resolved_db_path()) as conn:
        row = conn.execute(
            "SELECT balance_cents FROM wallets WHERE wallet_id = ?",
            (wallet_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def _related_tx_sum(payments_mod, charge_tx_id: str) -> int:
    """Return sum of (charge itself + all txs with related_tx_id = charge)."""
    import sqlite3
    with sqlite3.connect(payments_mod._resolved_db_path()) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM transactions
            WHERE tx_id = ? OR related_tx_id = ?
            """,
            (charge_tx_id, charge_tx_id),
        ).fetchone()
    return int(row[0] or 0)


def _total_wallet_balance(payments_mod) -> int:
    import sqlite3
    with sqlite3.connect(payments_mod._resolved_db_path()) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS total FROM wallets"
        ).fetchone()
    return int(row[0] or 0)


@pytest.fixture()
def payments_db(tmp_path, monkeypatch):
    from core import payments

    db_path = str(tmp_path / "payments-regressions.db")

    conn = getattr(getattr(payments, "_local", None), "conn", None)
    if conn is not None:
        conn.close()
        try:
            delattr(payments._local, "conn")
        except AttributeError:
            pass

    monkeypatch.setattr(payments, "DB_PATH", db_path)
    payments.init_payments_db()
    yield payments

    conn = getattr(getattr(payments, "_local", None), "conn", None)
    if conn is not None:
        conn.close()
        try:
            delattr(payments._local, "conn")
        except AttributeError:
            pass


def test_fix10_payout_curve_clawback_uses_supported_types_and_stays_zero_sum(payments_db):
    from core import payout_curve

    payments = payments_db
    caller_wallet = payments.get_or_create_wallet("user:caller")
    agent_wallet = payments.get_or_create_wallet("agent:test-agent")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    payments.deposit(caller_wallet["wallet_id"], 500, memo="test bootstrap")
    distribution = payments.compute_success_distribution(
        100, platform_fee_pct=10, fee_bearer_policy="caller"
    )
    caller_charge_cents = int(distribution["caller_charge_cents"])
    charge_tx_id = _direct_charge(
        payments, caller_wallet["wallet_id"], caller_charge_cents, "test-agent"
    )
    payments.post_call_payout(
        agent_wallet["wallet_id"],
        platform_wallet["wallet_id"],
        charge_tx_id,
        100,
        "test-agent",
        platform_fee_pct=10,
        fee_bearer_policy="caller",
    )

    result = payout_curve.apply_curve_clawback(
        job_id="job-payout-curve-1",
        agent_id="test-agent",
        agent_wallet_id=agent_wallet["wallet_id"],
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_payout_cents=100,
        payout_fraction=0.5,
    )
    assert result["applied"] is True
    assert result["clawback_cents"] == 50

    assert _wallet_balance(payments, caller_wallet["wallet_id"]) == 500 - 110 + 50
    assert _wallet_balance(payments, agent_wallet["wallet_id"]) == 100 - 50
    assert _wallet_balance(payments, platform_wallet["wallet_id"]) == 10
    assert _total_wallet_balance(payments) == 500

    import sqlite3
    with sqlite3.connect(payments._resolved_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT wallet_id, type, amount_cents
            FROM transactions
            WHERE memo = ?
            ORDER BY wallet_id, amount_cents
            """,
            ("payout_curve:job-payout-curve-1",),
        ).fetchall()
    assert rows == [
        (agent_wallet["wallet_id"], "charge", -50),
        (caller_wallet["wallet_id"], "refund", 50),
    ]

    second = payout_curve.apply_curve_clawback(
        job_id="job-payout-curve-1",
        agent_id="test-agent",
        agent_wallet_id=agent_wallet["wallet_id"],
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_payout_cents=100,
        payout_fraction=0.5,
    )
    assert second["applied"] is False
    assert second["reason"] == "already_applied"
    assert _total_wallet_balance(payments) == 500


def test_fix10_payout_curve_clawback_skips_cleanly_on_insufficient_balance(payments_db):
    from core import payout_curve

    payments = payments_db
    caller_wallet = payments.get_or_create_wallet("user:caller")
    agent_wallet = payments.get_or_create_wallet("agent:test-agent")

    result = payout_curve.apply_curve_clawback(
        job_id="job-payout-curve-2",
        agent_id="test-agent",
        agent_wallet_id=agent_wallet["wallet_id"],
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_payout_cents=100,
        payout_fraction=0.5,
    )
    assert result["applied"] is False
    assert result["reason"] == "insufficient_balance"
    assert _wallet_balance(payments, caller_wallet["wallet_id"]) == 0
    assert _wallet_balance(payments, agent_wallet["wallet_id"]) == 0

    import sqlite3
    with sqlite3.connect(payments._resolved_db_path()) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE memo = ?",
            ("payout_curve:job-payout-curve-2",),
        ).fetchone()[0]
    assert count == 0
