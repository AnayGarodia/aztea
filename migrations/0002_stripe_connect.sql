-- 0002_stripe_connect.sql
-- Add Stripe Connect Express account tracking to wallets.

ALTER TABLE wallets ADD COLUMN stripe_connect_account_id TEXT;
ALTER TABLE wallets ADD COLUMN stripe_connect_enabled INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS stripe_connect_transfers (
    transfer_id   TEXT PRIMARY KEY,
    wallet_id     TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,
    stripe_tx_id  TEXT NOT NULL,
    memo          TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_connect_transfers_wallet ON stripe_connect_transfers(wallet_id);
