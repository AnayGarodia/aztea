-- 0015_agent_identity.sql
-- Per-agent cryptographic identity. Each agent gets an Ed25519 keypair at
-- registration. The DID is derived from SERVER_BASE_URL at registration
-- time and frozen on the row so it survives later hostname changes.

ALTER TABLE agents ADD COLUMN did TEXT;
ALTER TABLE agents ADD COLUMN signing_public_key TEXT;
ALTER TABLE agents ADD COLUMN signing_private_key TEXT;
ALTER TABLE agents ADD COLUMN signing_alg TEXT NOT NULL DEFAULT 'ed25519';
ALTER TABLE agents ADD COLUMN signing_keys_created_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_did ON agents(did) WHERE did IS NOT NULL;

-- Signature attached to job output at completion time. NULL for legacy jobs
-- (and for any job whose agent does not yet have a signing keypair —
-- backfill is best-effort).
ALTER TABLE jobs ADD COLUMN output_signature TEXT;
ALTER TABLE jobs ADD COLUMN output_signature_alg TEXT;
ALTER TABLE jobs ADD COLUMN output_signed_by_did TEXT;
ALTER TABLE jobs ADD COLUMN output_signed_at TEXT;
