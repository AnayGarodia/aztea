-- 0046_wallet_holds.sql
-- Reserve-hold pattern for agent payouts.
--
-- The agent payout from a successful job is split into two pools:
--   * available portion -> immediately spendable / withdrawable
--   * held portion      -> reserved during the dispute window so a late
--                          rating or filed dispute always has funds to claw
--
-- Holds are released back into the available pool when:
--   * the dispute window closes with no clawback (sweeper)
--   * a 5-star (or curve-floor) rating arrives -> released cleanly
--   * a sub-floor rating arrives -> consumed (in part or whole)
--   * a dispute is filed       -> consumed by the dispute escrow
--
-- The transactions ledger remains insert-only. Hold lifecycle lives in
-- wallet_holds (mutable status), and wallets.held_cents is the cache that
-- mirrors SUM(amount_cents WHERE status='active') for that wallet.

CREATE TABLE IF NOT EXISTS wallet_holds (
    hold_id          TEXT PRIMARY KEY,
    wallet_id        TEXT NOT NULL REFERENCES wallets(wallet_id),
    job_id           TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at       TEXT NOT NULL,
    hold_until       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'released', 'clawed_full', 'clawed_partial')),
    released_at      TEXT NULL,
    clawback_cents   INTEGER NULL,
    release_reason   TEXT NULL
);

-- One hold per job, regardless of how settlement is replayed.
CREATE UNIQUE INDEX IF NOT EXISTS wallet_holds_job_uq
    ON wallet_holds(job_id);

-- Sweeper queries: active holds for a given wallet (rare) and active holds
-- whose window has closed (frequent — every sweeper tick).
CREATE INDEX IF NOT EXISTS wallet_holds_active_wallet_idx
    ON wallet_holds(wallet_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS wallet_holds_active_hold_until_idx
    ON wallet_holds(hold_until) WHERE status = 'active';

-- Wallet cache. held_cents = SUM(amount_cents WHERE status='active').
-- available_cents = balance_cents - held_cents is computed in code. Both
-- cached columns must move atomically with the wallet_holds row that
-- justifies the change, mirroring the balance_cents discipline.
ALTER TABLE wallets ADD COLUMN held_cents INTEGER NOT NULL DEFAULT 0;
