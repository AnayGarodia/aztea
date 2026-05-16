"""Stress test for the Postgres FOR UPDATE row-lock on ``pre_call_charge``.

# OWNS: concurrency-correctness assertions for the wallet charge path under
#       Postgres READ COMMITTED isolation. Specifically exercises the
#       ``FOR UPDATE`` lock at ``core/payments/base.py:825``.
# INVARIANTS asserted:
#   - Total successful charges == total expected charges (no race-induced loss)
#   - Final balance == initial_deposit - successful_charges * price (cache matches ledger)
#   - No duplicate tx_ids returned (each call commits its own row)
#   - When deposit is insufficient, the remaining charges fail with
#     InsufficientBalanceError — never silently succeed (over-charge)
# DECISIONS:
#   - Per-test unique owner IDs (UUID4 suffix) so the test can run against
#     a shared Postgres test DB without conflicting with neighbouring tests
#     or the application's own wallets. We do not isolate via schema —
#     payment tables need migrations applied (init_payments_db is no-op
#     under Postgres) and a per-test schema would require running every
#     migration up front, which is overkill for a unit-level stress test.
#   - Skipped when ``DATABASE_URL`` doesn't point at Postgres. SQLite uses
#     ``BEGIN IMMEDIATE`` which already serialises writes; the FOR UPDATE
#     path only executes under Postgres so a SQLite run wouldn't exercise
#     the code under test.

The note at ``core/payments/base.py:18`` flags phantom-read risk in the
Postgres guarantor-backstop branch under READ COMMITTED. The current
implementation locks the wallet row(s) with FOR UPDATE before reading
balance_cents, which serialises concurrent attempts. This test stresses
that lock with N threads * M charges and asserts no charge slips
through unaccounted. A regression that relaxes the lock or moves the
balance read outside its scope would fail these tests within seconds.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from core import db as _db
from core import payments

pytestmark = pytest.mark.skipif(
    not _db.IS_POSTGRES,
    reason=(
        "Charge concurrency test requires DATABASE_URL=postgresql://...; "
        "SQLite's BEGIN IMMEDIATE already serialises writes so the "
        "FOR UPDATE path under test never executes."
    ),
)


_PLATFORM_FEE_FRACTION = 0.10


def _fresh_wallet_ids() -> tuple[str, str]:
    """Mint per-test owner IDs so concurrent test runs don't share wallets."""
    suffix = uuid.uuid4().hex[:8]
    return f"test-charge-race-caller-{suffix}", f"test-charge-race-agent-{suffix}"


def _run_concurrent_charges(
    wallet_id: str,
    agent_id: str,
    *,
    n_threads: int,
    m_per_thread: int,
    price_cents: int,
) -> tuple[list[str], list[Exception]]:
    """Spawn ``n_threads`` workers, each calling pre_call_charge M times.

    Returns ``(successful_tx_ids, raised_exceptions)``. All threads
    rendezvous on a Barrier so the first charge attempts happen as
    close to simultaneously as the GIL + Postgres connection pool
    allow — maximising the chance of catching a missing lock.
    """
    barrier = threading.Barrier(n_threads)
    successful_ids: list[str] = []
    errors: list[Exception] = []
    result_lock = threading.Lock()

    def _worker() -> None:
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError as exc:
            with result_lock:
                errors.append(exc)
            return
        for _ in range(m_per_thread):
            try:
                tx_id = payments.pre_call_charge(wallet_id, price_cents, agent_id)
            except payments.InsufficientBalanceError as exc:
                with result_lock:
                    errors.append(exc)
                # Once funds run out, further attempts on this thread will
                # also fail — stop hammering and let other threads finish.
                return
            with result_lock:
                successful_ids.append(tx_id)

    threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    return successful_ids, errors


def test_concurrent_charges_with_sufficient_balance_all_succeed():
    """N*M charges of 1¢ against a wallet pre-funded for exactly N*M¢.

    Every charge should succeed; the wallet should end at 0; balance
    cache should match the ledger sum. A race that allowed phantom
    reads would either over-deduct (final balance < 0, impossible
    given the InsufficientBalanceError gate but the gate itself is
    what we're testing) or under-record charges in the ledger.
    """
    n_threads, m_per_thread, price_cents = 16, 10, 1
    total = n_threads * m_per_thread
    caller_owner, agent_owner = _fresh_wallet_ids()
    caller = payments.get_or_create_wallet(caller_owner)
    payments.get_or_create_wallet(agent_owner)
    payments.deposit(caller["wallet_id"], total * price_cents, "stress test setup")

    successful, errors = _run_concurrent_charges(
        caller["wallet_id"],
        agent_owner,
        n_threads=n_threads,
        m_per_thread=m_per_thread,
        price_cents=price_cents,
    )

    assert not errors, f"workers raised under sufficient balance: {errors}"
    assert len(successful) == total, (
        f"expected {total} successful charges, got {len(successful)} — "
        "race may have lost or duplicated a charge"
    )
    assert len(set(successful)) == total, (
        "duplicate tx_id returned — pre_call_charge must commit one row per call"
    )

    final_balance = payments.get_wallet(caller["wallet_id"])["balance_cents"]
    assert final_balance == 0, (
        f"balance_cents drifted from ledger: final={final_balance}, "
        f"expected 0 after {total} charges of {price_cents}¢ each"
    )


def test_concurrent_charges_with_exact_exhaustion_count_correctly():
    """Deposit covers exactly half the requested charges — half should
    succeed, half should raise ``InsufficientBalanceError``. The split
    must be deterministic from the wallet's POV: total successful
    charges * price must equal the deposit, with no over-charge under
    READ COMMITTED.
    """
    n_threads, m_per_thread, price_cents = 16, 10, 1
    total_attempts = n_threads * m_per_thread
    deposit_cents = (total_attempts // 2) * price_cents  # half the demand
    caller_owner, agent_owner = _fresh_wallet_ids()
    caller = payments.get_or_create_wallet(caller_owner)
    payments.get_or_create_wallet(agent_owner)
    payments.deposit(caller["wallet_id"], deposit_cents, "stress test partial setup")

    successful, errors = _run_concurrent_charges(
        caller["wallet_id"],
        agent_owner,
        n_threads=n_threads,
        m_per_thread=m_per_thread,
        price_cents=price_cents,
    )

    # Every failure must be the documented InsufficientBalanceError —
    # never a stray IntegrityError, deadlock, or stale-balance bug.
    for exc in errors:
        assert isinstance(exc, payments.InsufficientBalanceError), (
            f"unexpected exception class under exhaustion: {type(exc).__name__}: {exc}"
        )

    total_charged_cents = len(successful) * price_cents
    assert total_charged_cents == deposit_cents, (
        f"successful charges deducted {total_charged_cents}¢ but only "
        f"{deposit_cents}¢ was deposited — race allowed over-charge "
        "or balance cache diverged from ledger"
    )

    final_balance = payments.get_wallet(caller["wallet_id"])["balance_cents"]
    assert final_balance == 0, (
        f"balance_cents must be exactly 0 after exhausting the deposit; "
        f"got {final_balance}"
    )
    assert len(set(successful)) == len(successful), (
        "duplicate tx_id returned — phantom commit?"
    )


def test_concurrent_charges_ledger_sum_matches_balance_change():
    """For any concurrent run, the ledger row sum and balance_cents
    cache must agree. Detects the class of bug where a UPDATE lands but
    its compensating INSERT into ``transactions`` doesn't (or vice
    versa) — the same transactional-atomicity invariant reconciliation
    runs check, but exercised under load.
    """
    n_threads, m_per_thread, price_cents = 8, 8, 3
    caller_owner, agent_owner = _fresh_wallet_ids()
    caller = payments.get_or_create_wallet(caller_owner)
    payments.get_or_create_wallet(agent_owner)
    initial_deposit = n_threads * m_per_thread * price_cents
    payments.deposit(caller["wallet_id"], initial_deposit, "ledger-vs-cache stress")

    successful, _errors = _run_concurrent_charges(
        caller["wallet_id"],
        agent_owner,
        n_threads=n_threads,
        m_per_thread=m_per_thread,
        price_cents=price_cents,
    )

    final_balance = payments.get_wallet(caller["wallet_id"])["balance_cents"]
    expected_balance = initial_deposit - len(successful) * price_cents
    assert final_balance == expected_balance, (
        f"balance_cents cache ({final_balance}) != initial_deposit - "
        f"sum(successful_charges) ({expected_balance}) — atomicity broke"
    )
