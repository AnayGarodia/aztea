-- 0059_job_caller_escrow.sql
-- Caller-side escrow for async jobs.
--
-- Current behavior (pre-2026-05-18, deep tier D5):
--   POST /jobs immediately debits caller_wallet.balance_cents and stores
--   the charge_tx_id on the job. Refunds run on failure. A developer
--   expecting "money held until the job completes" semantics is surprised
--   when their balance drops the moment they create a job — even if the
--   agent does nothing.
--
-- New behavior (feature-flag AZTEA_CALLER_ESCROW_ENABLED=1):
--   POST /jobs creates a row in this table reserving the caller's funds
--   without modifying balance_cents. On job complete, the escrow is
--   consumed: balance debits, payout runs. On job failure / cancel /
--   timeout, the escrow is released and no debit occurs.
--
-- Kept separate from wallet_holds (the agent-side payout hold) to avoid
-- touching that production-critical money path. Both can be active at
-- once: caller_escrow during the job, then wallet_holds during the
-- dispute window. The table is intentionally tiny — the ledger remains
-- the source of truth for any real money movement.

CREATE TABLE IF NOT EXISTS job_caller_escrow (
    job_id           TEXT PRIMARY KEY REFERENCES jobs(job_id),
    caller_wallet_id TEXT NOT NULL REFERENCES wallets(wallet_id),
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','consumed','released')),
    resolved_at      TEXT,
    resolution_note  TEXT
);

CREATE INDEX IF NOT EXISTS job_caller_escrow_active_wallet_idx
    ON job_caller_escrow(caller_wallet_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS job_caller_escrow_expires_idx
    ON job_caller_escrow(expires_at) WHERE status = 'active';
