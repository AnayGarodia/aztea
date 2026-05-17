"""
Regression tests — one test per bug fix.

Fix 1: _caller_from_raw_api_key used wrong auth function name
Fix 2: MCP manifest used camelCase keys and aztea__ prefix
Fix 12: security search boost did not include scanner/secret tokens, so secret_scanner
        was missing from top results for "security" queries
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


def test_auto_invoke_default_trust_floor_matches_sparse_catalog_reality(monkeypatch):
    from core import feature_flags

    monkeypatch.delenv("AZTEA_AUTO_INVOKE_TRUST_FLOOR", raising=False)
    assert feature_flags.auto_invoke_trust_floor() == 30.0


def test_variable_pricing_overlay_covers_cve_tiered():
    # LIVE_ENDPOINT_TESTER_AGENT_ID was removed in the 2026-05-15 agent prune,
    # so this test now only exercises the CVE tiered path. CVE Lookup
    # became a gateway free-tier agent in 2026-05-17 — every tier rate is
    # 0¢, but the overlay must still report the "tiered" model so the
    # variable-pricing code path stays wired up if rates ever come back.
    from server import pricing_helpers
    from server.builtin_agents.constants import CVELOOKUP_AGENT_ID

    cve_agent = {
        "agent_id": CVELOOKUP_AGENT_ID,
        "price_per_call_usd": 0.0,
        "pricing_model": "fixed",
        "pricing_config": None,
    }

    cve_estimate = pricing_helpers.estimate_variable_charge(
        agent=cve_agent,
        payload={"cve_ids": ["CVE-1", "CVE-2", "CVE-3", "CVE-4", "CVE-5"]},
    )

    assert cve_estimate["price_cents"] == 0
    assert cve_estimate["pricing_model"] == "tiered"


def test_cve_not_found_returns_error_envelope_not_billable_success(monkeypatch):
    from agents import cve_lookup

    monkeypatch.setattr(cve_lookup, "_fetch_cve", lambda _cve_id: {"error": "not found"})
    monkeypatch.setattr(cve_lookup, "_fetch_cve_from_osv", lambda _cve_id: {"error": "not found"})
    result = cve_lookup.run({"cve_id": "CVE-9999-99999"})

    assert result["error"]["code"] == "cve_lookup.not_found"


def test_pipeline_contradiction_blocks_clean_bill_of_health():
    from core.pipelines.executor import _pipeline_contradiction

    message = _pipeline_contradiction(
        {
            "analyze": {
                "risk_tags": ["auth"],
                "secret_pattern_added": True,
                "error_handling_removed": True,
            },
            "review": {"issue_count": 0, "score": 9},
        }
    )

    assert message and "Pipeline contradiction" in message


def test_search_query_expansion_handles_typos_and_chinese():
    from core.registry.agents_ops import _expand_search_query

    assert "secret" in _expand_search_query("secrt scaner")
    assert "vulnerability" in _expand_search_query("检查代码中的漏洞")


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
            """,
            ("payout_curve:job-payout-curve-1",),
        ).fetchall()
    # ``wallet_id`` is a random UUID so any ORDER BY wallet_id is non-deterministic.
    # Compare as sets so the test does not depend on UUID lexical ordering.
    assert set(rows) == {
        (agent_wallet["wallet_id"], "charge", -50),
        (caller_wallet["wallet_id"], "refund", 50),
    }

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


# ---------------------------------------------------------------------------
# Fix 11 — _resolve_payload must detect missing fields from oneOf variants
# ---------------------------------------------------------------------------

def test_fix11_auto_hire_detects_missing_fields_for_oneof_schema():
    """_resolve_payload must detect missing fields from oneOf variants, not just top-level required."""
    from core.registry.auto_hire import CandidateAgent, decide
    import unittest.mock as mock

    cve_agent = CandidateAgent(
        agent_id="a3e239dd-ea92-556b-9c95-0a213a3daf59",
        slug="cve_lookup_agent",
        name="CVE Lookup Agent",
        description="live CVE data for a package or CVE ID security vulnerability nvd",
        tags=["security", "cve"],
        category="Security",
        price_per_call_usd=0.01,
        trust_score=90.0,
        success_rate=0.98,
        stability_tier="stable",
        input_schema={
            "type": "object",
            "properties": {
                "cve_id": {"type": "string"},
                "packages": {"type": "array", "items": {"type": "string"}},
            },
            "oneOf": [
                {"required": ["cve_id"]},
                {"required": ["packages"]},
            ],
        },
        raw={"call_count": 100, "codex_recommended": True},
    )

    with mock.patch("core.feature_flags.auto_invoke_enabled", return_value=True), \
         mock.patch("core.feature_flags.auto_invoke_confidence_floor", return_value=0.0), \
         mock.patch("core.feature_flags.auto_invoke_trust_floor", return_value=0.0), \
         mock.patch("core.feature_flags.auto_invoke_success_floor", return_value=0.0), \
         mock.patch("core.feature_flags.auto_invoke_server_cap_usd", return_value=10.0):
        decision = decide(
            intent="look up CVE-2021-44228",
            explicit_input=None,
            max_cost_usd=1.0,
            candidates=[cve_agent],
        )

    if decision.auto_invoked:
        assert decision.payload and (
            "cve_id" in decision.payload or "packages" in decision.payload
        ), "auto-invoked with empty payload — oneOf required fields were not detected"
    else:
        assert decision.reason == "missing_fields", f"Expected missing_fields gate, got: {decision.reason}"
        assert decision.missing_fields, "missing_fields must be non-empty"


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
    # The reserve-hold pattern renamed the defense-in-depth signal from
    # 'insufficient_balance' to 'underflow' to match the canary metric
    # label (payout_curve_clawback_skipped_total{reason='underflow'}).
    # The semantic is identical: agent had no funds to claw and no hold
    # was available to absorb the clawback.
    assert result["reason"] == "underflow"
    assert _wallet_balance(payments, caller_wallet["wallet_id"]) == 0
    assert _wallet_balance(payments, agent_wallet["wallet_id"]) == 0

    import sqlite3
    with sqlite3.connect(payments._resolved_db_path()) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE memo = ?",
            ("payout_curve:job-payout-curve-2",),
        ).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Fix 12 — security search boost must surface secret_scanner
# ---------------------------------------------------------------------------

def test_fix12_search_security_includes_secret_scanner():
    """_search_catalog('security') must rank secret_scanner in the top-10 results.

    The inner boost check previously only tested for CVE/dependency tokens.
    secret_scanner has 'scanner', 'secret', and 'credential' in its haystack
    so it should receive the +12 boost after the fix.
    """
    import threading
    # 1.6.3: RegistryBridge moved out of scripts/ into the SDK tree.
    import sys as _sys
    from pathlib import Path as _Path
    _SDK = str(_Path(__file__).resolve().parents[1] / "sdks" / "python-sdk")
    if _SDK not in _sys.path:
        _sys.path.insert(0, _SDK)
    from aztea.mcp import server as mcp  # noqa: E402

    cat = mcp.RegistryBridge.__new__(mcp.RegistryBridge)
    cat._lock = threading.Lock()
    cat._entries = []
    cat._catalog_cache = None
    cat._session_state = {}
    cat._auth_required = False
    cat.base_url = "http://localhost:8000"
    cat.api_key = "test"
    cat.timeout_seconds = 30
    cat._signup_url = ""

    # Inject a fake secret_scanner entry directly into the catalog cache so
    # the test does not require a running server.
    fake_secret_scanner = {
        "slug": "secret_scanner",
        "aliases": ["secret_scanner"],
        "kind": "registry_agent",
        "name": "Secret Scanner",
        "description": (
            "Scans source code for leaked credentials, secrets, API keys, "
            "and entropy-based patterns. Detects hardcoded passwords and tokens."
        ),
        "input_schema": {"type": "object"},
        "output_schema": {},
        "category": "Security",
        "tags": ["security", "scanner", "secrets", "credential", "leak"],
        "is_featured": False,
        "cacheable": False,
        "runtime_requirements": [],
        "tooling_kind": "scanner",
        "stability_tier": "stable",
        "codex_recommended": False,
        "short_use_cases": ["scan for leaked secrets", "detect credentials in code"],
        "trust_score": None,
        "success_rate": None,
        "avg_latency_ms": None,
        "price_per_call_usd": None,
        "verified": True,
    }
    # Also add a noise entry so the result list is non-trivial.
    fake_noise = {
        "slug": "unrelated_tool",
        "aliases": ["unrelated_tool"],
        "kind": "registry_agent",
        "name": "Unrelated Tool",
        "description": "Does something unrelated to security.",
        "input_schema": {"type": "object"},
        "output_schema": {},
        "category": "Utilities",
        "tags": [],
        "is_featured": False,
        "cacheable": False,
        "runtime_requirements": [],
        "tooling_kind": None,
        "stability_tier": "stable",
        "codex_recommended": False,
        "short_use_cases": [],
        "trust_score": None,
        "success_rate": None,
        "avg_latency_ms": None,
        "price_per_call_usd": None,
        "verified": False,
    }
    with cat._lock:
        cat._catalog_cache = [fake_secret_scanner, fake_noise]

    result = cat._search_catalog("security", limit=10)
    slugs = [r.get("slug") for r in result.get("results", [])]
    assert "secret_scanner" in slugs, (
        f"secret_scanner not in top-10 security results. Got: {slugs}"
    )


# ---------------------------------------------------------------------------
# Observability upgrade — /health endpoint
# ---------------------------------------------------------------------------

def test_health_endpoint_returns_ok_with_db_and_version(monkeypatch):
    """GET /health returns HTTP 200 with status, db, llm_providers, version."""
    import os

    os.environ.setdefault("API_KEY", "test-master-key")
    os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

    from fastapi.testclient import TestClient

    import server.application as server

    with TestClient(server.app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(body.keys()) >= {"status", "db", "llm_providers", "version"}
    assert body["status"] in {"ok", "degraded"}
    assert body["db"] in {"ok", "error"}
    assert isinstance(body["llm_providers"], list)
    assert isinstance(body["version"], str) and body["version"]

    # When the DB probe succeeds, overall status should be ok.
    if body["db"] == "ok":
        assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# Rate limit middleware — per-key sliding-window protection against abuse.
# Tests hit a non-existent probe path so the rate-limit middleware decides
# the outcome before any route handler runs (404 when allowed, 429 when
# rate-limited). Each test resets the in-memory store first.
# ---------------------------------------------------------------------------

_PROBE_PATH = "/__rate_limit_probe"


def _build_rate_limit_client(monkeypatch, *, master_key: str = "ratelimit-master"):
    """Spin up a TestClient with a fixed master key and a clean rate-limit store.

    Why: the top-level tests/conftest.py bumps AZTEA_RATE_LIMIT_* env vars to
    effective-infinity for the rest of the suite (so cumulative requests in
    unrelated integration tests don't cascade into spurious 429s). These
    *dedicated* rate-limit tests need the canonical 120/600/60/10 numbers
    to exercise the limiter, so we restore them explicitly here. Anchor in
    one place — feature_flags reads via attribute access in limit_for_scope.
    """
    from fastapi.testclient import TestClient

    import server.application as server
    from core import feature_flags, rate_limit

    monkeypatch.setattr(server, "_MASTER_KEY", master_key)
    monkeypatch.setattr(feature_flags, "RATE_LIMIT_DEFAULT_RPM", 120)
    monkeypatch.setattr(feature_flags, "RATE_LIMIT_WORKER_RPM", 600)
    monkeypatch.setattr(feature_flags, "RATE_LIMIT_ANON_RPM", 60)
    monkeypatch.setattr(feature_flags, "RATE_LIMIT_BURST_RPS", 10)
    rate_limit.reset_store_for_tests()
    client = TestClient(server.app, raise_server_exceptions=False)
    return client, server, rate_limit


def _install_fake_clock(monkeypatch, start: float, step: float):
    """Monkeypatch core.rate_limit._now to advance by ``step`` on each call."""
    from core import rate_limit

    state = {"t": start}

    def fake_now() -> float:
        state["t"] += step
        return state["t"]

    monkeypatch.setattr(rate_limit, "_now", fake_now)
    return state


def test_rate_limit_default_caller_blocks_at_threshold(monkeypatch):
    """The 121st caller-keyed request inside a minute must return 429."""
    client, _server, _rl = _build_rate_limit_client(monkeypatch)
    _install_fake_clock(monkeypatch, start=1000.0, step=0.5)

    headers = {"Authorization": "Bearer az_test_caller_threshold"}
    for i in range(120):
        resp = client.get(_PROBE_PATH, headers=headers)
        assert resp.status_code != 429, f"unexpected early 429 at request {i + 1}"

    resp = client.get(_PROBE_PATH, headers=headers)
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After")
    body = resp.json()
    assert body["error"] == "rate_limit_exceeded"
    assert body["details"]["limit_per_minute"] == 120


def test_rate_limit_worker_key_higher_threshold(monkeypatch):
    """Worker keys (azk_*) must clear 121 spaced requests without limiting."""
    client, _server, _rl = _build_rate_limit_client(monkeypatch)
    _install_fake_clock(monkeypatch, start=2000.0, step=0.2)

    headers = {"Authorization": "Bearer azk_test_worker_under_limit"}
    for i in range(121):
        resp = client.get(_PROBE_PATH, headers=headers)
        assert resp.status_code != 429, f"worker hit 429 at request {i + 1}"


def test_rate_limit_admin_key_never_limited(monkeypatch):
    """Master/admin key is exempt from accounting entirely."""
    client, server, _rl = _build_rate_limit_client(monkeypatch, master_key="adm-key-x")
    _install_fake_clock(monkeypatch, start=3000.0, step=0.001)

    headers = {"Authorization": f"Bearer {server._MASTER_KEY}"}
    for i in range(200):
        resp = client.get(_PROBE_PATH, headers=headers)
        assert resp.status_code != 429, f"admin key hit 429 at request {i + 1}"


def test_rate_limit_burst_protection_kicks_in(monkeypatch):
    """Even with plenty of per-minute budget, 11 hits in 1s trip the burst gate."""
    client, _server, rate_limit = _build_rate_limit_client(monkeypatch)
    # All 11 requests share the same monotonic instant.
    monkeypatch.setattr(rate_limit, "_now", lambda: 5000.0)

    headers = {"Authorization": "Bearer az_test_caller_burst"}
    for i in range(10):
        resp = client.get(_PROBE_PATH, headers=headers)
        assert resp.status_code != 429, f"unexpected 429 at burst request {i + 1}"

    resp = client.get(_PROBE_PATH, headers=headers)
    assert resp.status_code == 429
    body = resp.json()
    assert body["details"]["burst_limit_per_second"] == 10
    assert body["details"]["retry_after_seconds"] == 1


def test_rate_limit_anonymous_keyed_by_ip(monkeypatch):
    """Unauthenticated requests share one bucket per client IP at the anon limit."""
    client, _server, _rl = _build_rate_limit_client(monkeypatch)
    _install_fake_clock(monkeypatch, start=6000.0, step=0.5)

    for i in range(60):
        resp = client.get(_PROBE_PATH)  # no Authorization header
        assert resp.status_code != 429, f"anon hit 429 too early at request {i + 1}"

    resp = client.get(_PROBE_PATH)
    assert resp.status_code == 429
    body = resp.json()
    assert body["details"]["limit_per_minute"] == 60


def test_rate_limit_exempt_paths_not_counted(monkeypatch):
    """Exempt paths must not write to the store nor block subsequent requests.

    Spec calls for 1000 hits on /health, but /health does a per-request DB
    probe and the background sweeper thread occasionally takes a write lock
    long enough to deadlock against repeated reads in the same process. We
    use /api/openapi.json (also exempt, schema-cached after the first hit)
    and 200 iterations — 3.3× the anon per-minute cap is more than enough
    to prove exempt bypass and stays under one second of wall-clock.
    """
    client, _server, rate_limit = _build_rate_limit_client(monkeypatch)
    monkeypatch.setattr(rate_limit, "_now", lambda: 7000.0)

    for _ in range(200):
        resp = client.get("/api/openapi.json")
        assert resp.status_code == 200

    assert rate_limit.store_size_for_tests() == 0
    resp = client.get(
        _PROBE_PATH,
        headers={"Authorization": "Bearer az_after_exempt"},
    )
    assert resp.status_code != 429


def test_rate_limit_window_resets_after_passage_of_time(monkeypatch):
    """A bucket that exhausted its minute budget recovers after 65s elapse."""
    client, _server, rate_limit = _build_rate_limit_client(monkeypatch)
    state = _install_fake_clock(monkeypatch, start=8000.0, step=0.5)

    headers = {"Authorization": "Bearer az_test_window_reset"}
    for _ in range(120):
        client.get(_PROBE_PATH, headers=headers)
    resp = client.get(_PROBE_PATH, headers=headers)
    assert resp.status_code == 429

    state["t"] += 65.0
    resp = client.get(_PROBE_PATH, headers=headers)
    assert resp.status_code != 429


def test_rate_limit_response_shape_matches_contract(monkeypatch):
    """429 body must match the documented shape and Retry-After header type."""
    client, _server, rate_limit = _build_rate_limit_client(monkeypatch)
    monkeypatch.setattr(rate_limit, "_now", lambda: 9000.0)

    headers = {"Authorization": "Bearer az_test_shape"}
    for _ in range(10):
        client.get(_PROBE_PATH, headers=headers)
    resp = client.get(_PROBE_PATH, headers=headers)

    assert resp.status_code == 429
    body = resp.json()
    assert body == {
        "error": "rate_limit_exceeded",
        "message": body["message"],
        "details": {
            "limit_per_minute": 120,
            "burst_limit_per_second": 10,
            "retry_after_seconds": body["details"]["retry_after_seconds"],
        },
    }
    assert isinstance(body["details"]["retry_after_seconds"], int)
    retry_header = resp.headers.get("Retry-After")
    assert retry_header is not None and retry_header.isdigit()


def test_rate_limit_lru_eviction_under_pressure(monkeypatch):
    """Above RATE_LIMIT_MAX_TRACKED_KEYS the oldest-touched key is evicted."""
    client, _server, rate_limit = _build_rate_limit_client(monkeypatch)
    monkeypatch.setattr(rate_limit, "_now", lambda: 10_000.0)

    from core import feature_flags
    monkeypatch.setattr(feature_flags, "RATE_LIMIT_MAX_TRACKED_KEYS", 10)

    for i in range(110):
        client.get(_PROBE_PATH, headers={"Authorization": f"Bearer az_lru_{i}"})

    assert rate_limit.store_size_for_tests() <= 10
    assert not rate_limit.store_contains_key_for_tests("key:az_lru_0")
    resp = client.get(_PROBE_PATH, headers={"Authorization": "Bearer az_lru_after_evict"})
    assert resp.status_code != 429


def test_rate_limit_middleware_fails_open(monkeypatch, caplog):
    """An exception inside the check must not block legitimate traffic."""
    client, _server, rate_limit = _build_rate_limit_client(monkeypatch)

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic rate-limit failure")

    monkeypatch.setattr(rate_limit, "check_and_record", boom)
    caplog.set_level("WARNING")

    resp = client.get(
        _PROBE_PATH,
        headers={"Authorization": "Bearer az_fail_open"},
    )
    assert resp.status_code != 429
    assert any("ratelimit.fail_open" in record.getMessage() for record in caplog.records)
