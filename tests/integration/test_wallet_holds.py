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
    monkeypatch.setattr(migrate, "DB_PATH", str(db_path))
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
# 2-5: lifecycle integration (added in later commits as the surface lands)
# ---------------------------------------------------------------------------
# Settlement / withdrawal / clawback / sweeper coverage will be added
# alongside the corresponding implementation commits so each commit ships
# with a green test for its slice.
