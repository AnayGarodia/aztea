-- 0080_observation_receipts.sql
--
-- Phase 2 of the agent-readable-web build: proof-of-observation receipts.
-- A signed statement of PROVENANCE (not truth): what was observed, at which
-- URL, when, against which DOM hash, by which did:web identity. A downstream
-- buyer verifies the signature offline without re-crawling. Insert-only.
-- No FK on job_id/agent_id so a receipt stays verifiable after an agent is
-- deleted (receipts are preserved by design). observed_at is server-stamped.

CREATE TABLE IF NOT EXISTS observation_receipts (
    receipt_id        TEXT PRIMARY KEY,        -- 'obs_' + 22 base62
    job_id            TEXT NOT NULL DEFAULT '',
    agent_id          TEXT NOT NULL,
    signer_kind       TEXT NOT NULL DEFAULT 'agent'
        CHECK (signer_kind IN ('agent','server_sealer')),
    signer_did        TEXT NOT NULL,
    observed_at       INTEGER NOT NULL,        -- server epoch seconds
    request_url       TEXT NOT NULL,
    final_url         TEXT NOT NULL,
    http_status       INTEGER,
    content_type      TEXT,
    snapshot_kind     TEXT NOT NULL DEFAULT 'accessibility_tree',
    dom_sha256        TEXT NOT NULL,
    dom_bytes         INTEGER NOT NULL DEFAULT 0,
    extraction_sha256 TEXT NOT NULL,
    extraction_json   TEXT,
    signature         TEXT NOT NULL,           -- base64 raw Ed25519 over the sigil
    signature_alg     TEXT NOT NULL DEFAULT 'Ed25519',
    schema_version    TEXT NOT NULL DEFAULT 'aztea/observation-receipt/1',
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observation_receipts_job ON observation_receipts(job_id);
CREATE INDEX IF NOT EXISTS idx_observation_receipts_agent ON observation_receipts(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_observation_receipts_url ON observation_receipts(final_url);
