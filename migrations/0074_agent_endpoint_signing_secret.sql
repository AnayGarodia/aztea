-- 0074_agent_endpoint_signing_secret.sql
--
-- Per-agent shared secret used to HMAC-sign outbound calls from Aztea to the
-- agent's endpoint_url. Sellers receive this once at registration and verify
-- it on every inbound request. Without verification, a URL leak would let a
-- freeloader call the seller directly and bypass billing.
--
-- The receipt-signing Ed25519 keypair (migration 0015) signs in the OTHER
-- direction (Aztea to buyer). This secret signs Aztea to seller. Both
-- layers coexist.
--
-- NULL values are allowed for back-compat. Legacy agents registered before
-- this migration will fall through to unsigned dispatch in the call path
-- until they rotate (POST /registry/agents/{id}/rotate-secret) or the
-- backfill job assigns one.

ALTER TABLE agents ADD COLUMN endpoint_signing_secret TEXT;
ALTER TABLE agents ADD COLUMN endpoint_signing_secret_rotated_at TEXT;

CREATE INDEX IF NOT EXISTS idx_agents_endpoint_signing_secret_rotated_at
  ON agents(endpoint_signing_secret_rotated_at);
