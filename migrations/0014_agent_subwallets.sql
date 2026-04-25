-- 0014_agent_subwallets.sql
-- Make per-agent wallets first-class: link them to their owner wallet,
-- store guarantor policy and a display label.

ALTER TABLE wallets ADD COLUMN parent_wallet_id TEXT REFERENCES wallets(wallet_id);
ALTER TABLE wallets ADD COLUMN guarantor_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE wallets ADD COLUMN guarantor_cap_cents INTEGER;
ALTER TABLE wallets ADD COLUMN display_label TEXT;

CREATE INDEX IF NOT EXISTS idx_wallets_parent_wallet_id ON wallets(parent_wallet_id);

-- Backfill: every wallet with owner_id starting with 'agent:' gets its
-- parent_wallet_id set to the agent's owner's wallet.
UPDATE wallets
SET parent_wallet_id = (
    SELECT w2.wallet_id
    FROM agents a
    JOIN wallets w2 ON w2.owner_id = a.owner_id
    WHERE 'agent:' || a.agent_id = wallets.owner_id
)
WHERE wallets.owner_id LIKE 'agent:%'
  AND wallets.parent_wallet_id IS NULL;
