-- Add integer cents column alongside existing REAL column, backfill, then use it
ALTER TABLE agents ADD COLUMN price_per_call_cents INTEGER;
UPDATE agents SET price_per_call_cents = CAST(ROUND(price_per_call_usd * 100) AS INTEGER)
  WHERE price_per_call_usd IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agents_price_cents ON agents(price_per_call_cents);
