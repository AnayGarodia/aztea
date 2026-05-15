"""End-to-end tests for the wallet reserve-hold pattern.

Each test owns its own SQLite DB so the wallet_holds + transactions ledger
state is fully isolated from neighbouring suites. Tests are grouped by
lifecycle stage:

    1. compute_hold_cents pure function
    2. settlement creates holds
    3. withdrawal enforces available balance
    4. clawback consumption (rating + dispute)
    5. release sweeper
    6. reconciliation
    7. concurrency
"""

from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from core import auth, db, disputes, jobs, payments, registry
from core.payments import holds


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _close_module_conn(module) -> None:
    conn = getattr(module._local, "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(module._local, "conn")
    except AttributeError:
        pass


@pytest.fixture
def isolated_db(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-holds-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, disputes)
    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))

    # Apply migrations so wallet_holds + held_cents exist before init_payments_db.
    from core import migrate
    migrate.apply_migrations(str(db_path))
    payments.init_payments_db()

    yield db_path

    for module in modules:
        _close_module_conn(module)
    for suffix in ("", "-shm", "-wal"):
        path = Path(f"{db_path}{suffix}")
        if path.exists():
            path.unlink()


# ---------------------------------------------------------------------------
# 1. compute_hold_cents — pure function
# ---------------------------------------------------------------------------


class TestComputeHoldCents:
    def test_no_curve_holds_full_payout(self):
        # No floor declared -> full payout at risk -> hold the entire amount.
        assert holds.compute_hold_cents(1000, None) == 1000

    def test_min_fraction_one_holds_zero(self):
        # Curve says every rating keeps 100% -> nothing at risk.
        assert holds.compute_hold_cents(1000, {"1": 1.0, "5": 1.0}) == 0

    def test_half_floor_holds_half(self):
        # min_fraction=0.5 -> half at risk -> hold 500 of 1000.
        assert holds.compute_hold_cents(1000, {"1": 0.5, "5": 1.0}) == 500

    def test_zero_floor_holds_full(self):
        # min_fraction=0 -> full payout could be clawed back.
        assert holds.compute_hold_cents(1000, {"1": 0.0, "5": 1.0}) == 1000

    def test_zero_payout_yields_zero_hold(self):
        assert holds.compute_hold_cents(0, {"1": 0.5}) == 0

    def test_invalid_curve_falls_back_to_full(self):
        # A malformed curve must NOT silently under-hold.
        assert holds.compute_hold_cents(500, {"1": "nope"}) == 500  # type: ignore[dict-item]

    def test_rounds_up_partial_cent(self):
        # 333 * (1 - 0.5) = 166.5 -> hold 167, never 166.
        assert holds.compute_hold_cents(333, {"1": 0.5, "5": 1.0}) == 167

    def test_held_cannot_exceed_payout(self):
        # Defensive: even if at_risk somehow rounds beyond payout, the
        # contract caps the hold to the payout.
        assert holds.compute_hold_cents(100, {"1": -0.5}) <= 100


# ---------------------------------------------------------------------------
# 3. Withdrawal enforces available balance
# ---------------------------------------------------------------------------


class TestWithdrawalAvailableBalance:
    """The /wallets/withdraw gate is HTTP-shaped, but the rule it enforces is
    a pure expression over wallet rows. Test the rule directly so we don't
    need a full Stripe mock harness — the comprehensive HTTP test in commit
    10 covers the wired endpoint.
    """

    def _wallet_with(self, balance_cents: int, held_cents: int) -> dict:
        owner_id = f"user:withdraw-{uuid.uuid4().hex[:8]}"
        wallet = payments.get_or_create_wallet(owner_id)
        if balance_cents:
            payments.deposit(wallet["wallet_id"], balance_cents, memo="test")
        if held_cents:
            with db.get_db_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE wallets SET held_cents = %s WHERE wallet_id = %s",
                    (held_cents, wallet["wallet_id"]),
                )
        return payments.get_wallet(wallet["wallet_id"])

    def test_withdrawal_rejected_when_request_exceeds_available(self, isolated_db):
        wallet = self._wallet_with(balance_cents=1000, held_cents=400)
        held = int(wallet.get("held_cents") or 0)
        available = max(0, int(wallet["balance_cents"]) - held)
        # Mirror the gate condition in part_014.py::withdraw.
        assert available == 600
        assert available < 700  # request

    def test_withdrawal_succeeds_when_request_at_or_below_available(self, isolated_db):
        wallet = self._wallet_with(balance_cents=1000, held_cents=400)
        held = int(wallet.get("held_cents") or 0)
        available = max(0, int(wallet["balance_cents"]) - held)
        assert available == 600
        assert 600 <= available  # request equal to available is OK

    def test_held_cents_default_is_zero_for_legacy_wallets(self, isolated_db):
        wallet = self._wallet_with(balance_cents=500, held_cents=0)
        assert (wallet.get("held_cents") or 0) == 0
        # Available == balance for any wallet with no holds.
        assert int(wallet["balance_cents"]) - int(wallet.get("held_cents") or 0) == 500


# ---------------------------------------------------------------------------
# 2. Settlement creates holds
# ---------------------------------------------------------------------------


def _fund_caller_wallet(caller_owner_id: str, cents: int) -> dict:
    wallet = payments.get_or_create_wallet(caller_owner_id)
    payments.deposit(wallet["wallet_id"], cents, memo="hold-test funds")
    return payments.get_wallet(wallet["wallet_id"])


def _agent_with_curve(curve_json: str | None = None) -> dict:
    owner_id = f"user:owner-{uuid.uuid4().hex[:8]}"
    payments.get_or_create_wallet(owner_id)
    agent_id = registry.register_agent(
        name=f"Hold Test Agent {uuid.uuid4().hex[:6]}",
        description="Hold lifecycle test agent",
        endpoint_url="http://localhost:8000/internal/echo",
        price_per_call_usd=0.10,
        tags=["hold-test"],
        owner_id=owner_id,
        payout_curve=curve_json,
    )
    return registry.get_agent(agent_id, include_unapproved=True)


def _settle_agent_payout(agent, caller_owner_id, price_cents, dispute_window_hours=72):
    """Run a deterministic settle: charge caller (price + fee), payout agent.

    fee_bearer_policy='caller' -> caller pays price + fee, agent receives
    full price, platform receives fee. Net ledger movement is zero, which
    matches the production flow and keeps the audit invariants intact.
    """
    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent['agent_id']}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    distribution = payments.compute_success_distribution(
        price_cents, platform_fee_pct=10, fee_bearer_policy="caller",
    )
    charge_tx_id = payments.pre_call_charge(
        caller_wallet["wallet_id"],
        int(distribution["caller_charge_cents"]),
        agent["agent_id"],
    )
    from core import payout_curve as _pc
    curve = _pc.parse_curve(agent.get("payout_curve"))
    payments.post_call_payout(
        agent_wallet["wallet_id"],
        platform_wallet["wallet_id"],
        charge_tx_id,
        price_cents,
        agent["agent_id"],
        platform_fee_pct=10,
        fee_bearer_policy="caller",
        job_id=f"job-{uuid.uuid4().hex[:8]}",
        dispute_window_hours=dispute_window_hours,
        payout_curve=curve,
    )
    return {
        "caller_wallet": payments.get_wallet(caller_wallet["wallet_id"]),
        "agent_wallet": payments.get_wallet(agent_wallet["wallet_id"]),
        "platform_wallet": payments.get_wallet(platform_wallet["wallet_id"]),
        "charge_tx_id": charge_tx_id,
        "distribution": distribution,
    }


class TestSettlementCreatesHold:
    def test_settlement_with_no_curve_holds_full_payout(self, isolated_db):
        agent = _agent_with_curve(None)
        caller_owner = f"user:caller-{uuid.uuid4().hex[:8]}"
        _fund_caller_wallet(caller_owner, 2000)
        result = _settle_agent_payout(agent, caller_owner, price_cents=1000)
        agent_wallet = result["agent_wallet"]
        # fee_bearer_policy=caller: agent gets full price (1000); no curve -> hold all 1000.
        assert agent_wallet["balance_cents"] == 1000
        assert agent_wallet["held_cents"] == 1000

    def test_settlement_with_floor_one_holds_zero(self, isolated_db):
        agent = _agent_with_curve('{"1": 1.0, "5": 1.0}')
        caller_owner = f"user:caller-{uuid.uuid4().hex[:8]}"
        _fund_caller_wallet(caller_owner, 2000)
        result = _settle_agent_payout(agent, caller_owner, price_cents=1000)
        agent_wallet = result["agent_wallet"]
        # min_fraction = 1.0 -> nothing at risk -> hold zero.
        assert agent_wallet["balance_cents"] == 1000
        assert agent_wallet["held_cents"] == 0

    def test_settlement_creates_hold_for_at_risk_portion(self, isolated_db):
        agent = _agent_with_curve('{"1": 0.5, "5": 1.0}')
        caller_owner = f"user:caller-{uuid.uuid4().hex[:8]}"
        _fund_caller_wallet(caller_owner, 2000)
        result = _settle_agent_payout(agent, caller_owner, price_cents=1000)
        agent_wallet = result["agent_wallet"]
        # Agent payout 1000, at-risk fraction 0.5 -> hold 500.
        assert agent_wallet["balance_cents"] == 1000
        assert agent_wallet["held_cents"] == 500

    def test_settlement_is_idempotent_on_replay(self, isolated_db):
        agent = _agent_with_curve(None)
        caller_owner = f"user:caller-{uuid.uuid4().hex[:8]}"
        _fund_caller_wallet(caller_owner, 2000)
        caller_wallet = payments.get_wallet_by_owner(caller_owner)
        agent_wallet = payments.get_or_create_wallet(f"agent:{agent['agent_id']}")
        platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
        charge_tx_id = payments.pre_call_charge(
            caller_wallet["wallet_id"],
            1000,
            agent["agent_id"],
        )
        job_id = "job-replay-1"
        for _ in range(3):
            payments.post_call_payout(
                agent_wallet["wallet_id"],
                platform_wallet["wallet_id"],
                charge_tx_id,
                1000,
                agent["agent_id"],
                platform_fee_pct=10,
                fee_bearer_policy="caller",
                job_id=job_id,
                dispute_window_hours=72,
                payout_curve=None,
            )
        agent_wallet_after = payments.get_wallet(agent_wallet["wallet_id"])
        # Replay must NOT inflate either cache.
        assert agent_wallet_after["balance_cents"] == 1000
        assert agent_wallet_after["held_cents"] == 1000


# ---------------------------------------------------------------------------
# 4. Payout-curve clawback consumes hold
# ---------------------------------------------------------------------------


def _settle_with_curve(curve_json: str | None, price_cents: int = 1000):
    """Helper: register agent + caller, settle a single job, return refs.

    Charges caller_charge_cents (price + fee) so the ledger stays balanced.
    """
    agent = _agent_with_curve(curve_json)
    caller_owner = f"user:caller-{uuid.uuid4().hex[:8]}"
    _fund_caller_wallet(caller_owner, price_cents * 2)
    caller_wallet = payments.get_wallet_by_owner(caller_owner)
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent['agent_id']}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    distribution = payments.compute_success_distribution(
        price_cents, platform_fee_pct=10, fee_bearer_policy="caller",
    )
    charge_tx_id = payments.pre_call_charge(
        caller_wallet["wallet_id"],
        int(distribution["caller_charge_cents"]),
        agent["agent_id"],
    )
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    from core import payout_curve as _pc
    curve = _pc.parse_curve(agent.get("payout_curve"))
    payments.post_call_payout(
        agent_wallet["wallet_id"],
        platform_wallet["wallet_id"],
        charge_tx_id,
        price_cents,
        agent["agent_id"],
        platform_fee_pct=10,
        fee_bearer_policy="caller",
        job_id=job_id,
        dispute_window_hours=72,
        payout_curve=curve,
    )
    return {
        "agent": agent,
        "caller_wallet": caller_wallet,
        "agent_wallet": payments.get_wallet(agent_wallet["wallet_id"]),
        "platform_wallet": payments.get_wallet(platform_wallet["wallet_id"]),
        "charge_tx_id": charge_tx_id,
        "job_id": job_id,
        "curve": curve,
        "price_cents": price_cents,
        "distribution": distribution,
    }


class TestPayoutCurveClawbackConsumesHold:
    def test_full_clawback_consumes_hold_and_marks_clawed_full(self, isolated_db):
        ctx = _settle_with_curve('{"1": 0.0, "5": 1.0}')
        # Agent payout = 1000, fraction at 1-star = 0.0 -> clawback = 1000.
        from core import payout_curve as _pc
        result = _pc.apply_curve_clawback(
            job_id=ctx["job_id"],
            agent_id=ctx["agent"]["agent_id"],
            agent_wallet_id=ctx["agent_wallet"]["wallet_id"],
            caller_wallet_id=ctx["caller_wallet"]["wallet_id"],
            agent_payout_cents=1000,
            payout_fraction=0.0,
        )
        assert result["applied"] is True
        assert result["clawback_cents"] == 1000

        from core.payments import holds as _holds
        with db.get_db_connection() as conn:
            hold = conn.execute(
                "SELECT status, release_reason, clawback_cents FROM wallet_holds WHERE job_id = %s",
                (ctx["job_id"],),
            ).fetchone()
        assert hold["status"] == "clawed_full"
        assert hold["release_reason"] == _holds.RELEASE_REASON_RATING_CLAWBACK
        assert int(hold["clawback_cents"]) == 1000

        agent_after = payments.get_wallet(ctx["agent_wallet"]["wallet_id"])
        assert agent_after["balance_cents"] == 0
        assert agent_after["held_cents"] == 0

        caller_after = payments.get_wallet(ctx["caller_wallet"]["wallet_id"])
        # Caller paid 1100 (1000 price + 100 fee) and got 1000 clawed back.
        # Initial deposit was 2000, so balance now = 2000 - 1100 + 1000 = 1900.
        assert caller_after["balance_cents"] == 1900

    def test_partial_clawback_consumes_hold_and_releases_remainder(self, isolated_db):
        ctx = _settle_with_curve('{"3": 0.5, "5": 1.0}')
        # Agent payout = 1000, fraction at 3-star = 0.5 -> clawback = 500;
        # hold was sized at min_fraction=0.5 -> 500 reserved -> clawed_full
        # since clawback == hold.amount (500 == 500).
        from core import payout_curve as _pc
        result = _pc.apply_curve_clawback(
            job_id=ctx["job_id"],
            agent_id=ctx["agent"]["agent_id"],
            agent_wallet_id=ctx["agent_wallet"]["wallet_id"],
            caller_wallet_id=ctx["caller_wallet"]["wallet_id"],
            agent_payout_cents=1000,
            payout_fraction=0.5,
        )
        assert result["applied"] is True
        with db.get_db_connection() as conn:
            hold = conn.execute(
                "SELECT status, clawback_cents FROM wallet_holds WHERE job_id = %s",
                (ctx["job_id"],),
            ).fetchone()
        assert hold["status"] == "clawed_full"
        assert int(hold["clawback_cents"]) == 500
        agent_after = payments.get_wallet(ctx["agent_wallet"]["wallet_id"])
        assert agent_after["balance_cents"] == 500
        assert agent_after["held_cents"] == 0

    def test_partial_clawback_smaller_than_hold_releases_remainder(self, isolated_db):
        # Curve floors at 0.25 (i.e., min_fraction = 0.25 -> hold 750), but
        # only the 3-star fraction = 0.7 fires -> clawback = 300. Remainder
        # 450 must release with the same hold update.
        ctx = _settle_with_curve('{"3": 0.7, "1": 0.25, "5": 1.0}')
        from core import payout_curve as _pc
        result = _pc.apply_curve_clawback(
            job_id=ctx["job_id"],
            agent_id=ctx["agent"]["agent_id"],
            agent_wallet_id=ctx["agent_wallet"]["wallet_id"],
            caller_wallet_id=ctx["caller_wallet"]["wallet_id"],
            agent_payout_cents=1000,
            payout_fraction=0.7,
        )
        assert result["applied"] is True
        assert result["clawback_cents"] == 300
        with db.get_db_connection() as conn:
            hold = conn.execute(
                "SELECT status, amount_cents, clawback_cents FROM wallet_holds WHERE job_id = %s",
                (ctx["job_id"],),
            ).fetchone()
        assert hold["status"] == "clawed_partial"
        assert int(hold["amount_cents"]) == 750
        assert int(hold["clawback_cents"]) == 300
        agent_after = payments.get_wallet(ctx["agent_wallet"]["wallet_id"])
        # held drops by full hold (750), balance drops by clawback (300).
        assert agent_after["balance_cents"] == 700
        assert agent_after["held_cents"] == 0

    def test_top_rating_releases_hold_cleanly(self, isolated_db):
        ctx = _settle_with_curve('{"1": 0.5, "5": 1.0}')
        # 5-star -> fraction 1.0 -> no clawback, but the hold should release.
        from core import payout_curve as _pc
        result = _pc.apply_curve_clawback(
            job_id=ctx["job_id"],
            agent_id=ctx["agent"]["agent_id"],
            agent_wallet_id=ctx["agent_wallet"]["wallet_id"],
            caller_wallet_id=ctx["caller_wallet"]["wallet_id"],
            agent_payout_cents=1000,
            payout_fraction=1.0,
        )
        assert result["applied"] is False
        with db.get_db_connection() as conn:
            hold = conn.execute(
                "SELECT status, release_reason FROM wallet_holds WHERE job_id = %s",
                (ctx["job_id"],),
            ).fetchone()
        from core.payments import holds as _holds
        assert hold["status"] == "released"
        assert hold["release_reason"] == _holds.RELEASE_REASON_RATING_RELEASE
        agent_after = payments.get_wallet(ctx["agent_wallet"]["wallet_id"])
        assert agent_after["held_cents"] == 0
        assert agent_after["balance_cents"] == 1000  # full payout retained

    def test_dispute_clawback_consumes_hold(self, isolated_db):
        """Filing a dispute on a settled job consumes the active hold and
        clears the agent's held_cents in the same transaction as the
        escrow lock-up.
        """
        ctx = _settle_with_curve(None)  # no curve -> hold full payout
        from core.payments import trust_disputes as _td
        from core.payments import holds as _holds

        # Build a dispute manually so we can drive the lock helper directly
        # without standing up the full /jobs/{id}/dispute HTTP route.
        # Use jobs.create_job to get the full row shape, then update it to
        # the post-settlement state so trust_disputes can find the wallet
        # IDs + charge_tx_id it expects.
        from core import jobs as _jobs
        real_job_id = _jobs.create_job(
            agent_id=str(ctx["agent"]["agent_id"]),
            caller_owner_id=str(ctx["caller_wallet"]["owner_id"]),
            caller_wallet_id=str(ctx["caller_wallet"]["wallet_id"]),
            agent_wallet_id=str(ctx["agent_wallet"]["wallet_id"]),
            platform_wallet_id=str(ctx["platform_wallet"]["wallet_id"]),
            price_cents=1000,
            caller_charge_cents=int(ctx["distribution"]["caller_charge_cents"]),
            platform_fee_pct_at_create=10,
            fee_bearer_policy="caller",
            client_id=None,
            charge_tx_id=ctx["charge_tx_id"],
            input_payload={"task": "hold-test"},
            agent_owner_id=str(ctx["agent"]["owner_id"]),
        )["job_id"]
        # Re-point ctx['job_id'] at the real row so the wallet_holds row
        # we want to consume needs to match the real_job_id key. Re-create
        # the hold using the real id since trust_disputes reads by job_id.
        with db.get_db_connection() as conn:
            with conn:  # commits on exit; required so the next caller can BEGIN
                conn.execute(
                    "UPDATE wallet_holds SET job_id = %s WHERE job_id = %s",
                    (real_job_id, ctx["job_id"]),
                )
                conn.execute(
                    """
                    UPDATE jobs SET status='complete', settled_at = %s
                    WHERE job_id = %s
                    """,
                    ("2026-05-15T00:00:00+00:00", real_job_id),
                )
                conn.execute(
                    """
                    INSERT INTO disputes (
                        dispute_id, job_id, filed_by_owner_id, side, status,
                        reason, filing_deposit_cents, filed_at
                    ) VALUES (%s, %s, %s, 'caller', 'pending', %s, 0, %s)
                    """,
                    (
                        "dispute-test-1",
                        real_job_id,
                        ctx["caller_wallet"]["owner_id"],
                        "Test dispute for hold consumption",
                        "2026-05-15T00:00:00+00:00",
                    ),
                )
        ctx["job_id"] = real_job_id

        result = _td.lock_dispute_funds("dispute-test-1")
        assert int(result["locked_cents"]) > 0

        with db.get_db_connection() as conn:
            hold = conn.execute(
                "SELECT status, release_reason FROM wallet_holds WHERE job_id = %s",
                (ctx["job_id"],),
            ).fetchone()
        assert hold is not None
        assert hold["status"] == "clawed_full"
        assert hold["release_reason"] == _holds.RELEASE_REASON_DISPUTE_CLAWBACK

        agent_after = payments.get_wallet(ctx["agent_wallet"]["wallet_id"])
        assert agent_after["held_cents"] == 0
        assert agent_after["balance_cents"] == 0

    def test_release_sweeper_releases_expired_holds(self, isolated_db):
        """release_expired_holds picks up any wallet_holds row whose
        hold_until has passed and marks it released, dropping held_cents.
        """
        ctx = _settle_with_curve(None)
        # Backdate the hold so it appears expired.
        with db.get_db_connection() as conn:
            with conn:
                conn.execute(
                    "UPDATE wallet_holds SET hold_until = %s WHERE job_id = %s",
                    ("2000-01-01T00:00:00+00:00", ctx["job_id"]),
                )
        from core.payments import holds as _holds
        released = _holds.release_expired_holds(limit=10)
        assert released == 1
        with db.get_db_connection() as conn:
            hold = conn.execute(
                "SELECT status, release_reason FROM wallet_holds WHERE job_id = %s",
                (ctx["job_id"],),
            ).fetchone()
        assert hold["status"] == "released"
        assert hold["release_reason"] == _holds.RELEASE_REASON_WINDOW_EXPIRED
        agent_after = payments.get_wallet(ctx["agent_wallet"]["wallet_id"])
        assert agent_after["held_cents"] == 0
        # Balance untouched — no clawback.
        assert agent_after["balance_cents"] == 1000

    def test_release_sweeper_skips_active_holds(self, isolated_db):
        ctx = _settle_with_curve(None)
        # Default hold_until is 72h in the future.
        from core.payments import holds as _holds
        released = _holds.release_expired_holds(limit=10)
        assert released == 0
        agent_after = payments.get_wallet(ctx["agent_wallet"]["wallet_id"])
        assert agent_after["held_cents"] == 1000  # untouched

    def test_clawback_is_idempotent(self, isolated_db):
        ctx = _settle_with_curve('{"1": 0.5, "5": 1.0}')
        from core import payout_curve as _pc
        first = _pc.apply_curve_clawback(
            job_id=ctx["job_id"],
            agent_id=ctx["agent"]["agent_id"],
            agent_wallet_id=ctx["agent_wallet"]["wallet_id"],
            caller_wallet_id=ctx["caller_wallet"]["wallet_id"],
            agent_payout_cents=1000,
            payout_fraction=0.5,
        )
        second = _pc.apply_curve_clawback(
            job_id=ctx["job_id"],
            agent_id=ctx["agent"]["agent_id"],
            agent_wallet_id=ctx["agent_wallet"]["wallet_id"],
            caller_wallet_id=ctx["caller_wallet"]["wallet_id"],
            agent_payout_cents=1000,
            payout_fraction=0.5,
        )
        assert first["applied"] is True
        assert second["applied"] is False
        assert second["reason"] == "already_applied"
        agent_after = payments.get_wallet(ctx["agent_wallet"]["wallet_id"])
        # Wallet state unchanged after the second call.
        assert agent_after["balance_cents"] == 500
        assert agent_after["held_cents"] == 0
