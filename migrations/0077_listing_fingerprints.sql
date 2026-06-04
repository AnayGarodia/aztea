-- 0077_listing_fingerprints.sql
--
-- Exact-content fingerprints for published listings. A fingerprint is a
-- SHA-256 over the boilerplate-stripped, whitespace-normalised body of a
-- hosted SKILL.md or a submitted Python handler. The publish path computes
-- the candidate fingerprint and refuses with listing.duplicate on an exact
-- match against an existing listing. This is the ONLY duplicate hard-block.
-- Embedding cosine similarity over agent_embeddings is advisory-only and
-- never blocks, so near-duplicates land in probation instead of being
-- refused. See core/listing_dedup.py and the 2026-06-03 publish-verification
-- review decision D2.
--
-- agent_id references the agent created at publish time. The fingerprint is
-- written AFTER a successful registration, so the candidate never matches
-- itself at publish-check time.
CREATE TABLE IF NOT EXISTS listing_fingerprints (
    agent_id    TEXT PRIMARY KEY REFERENCES agents(agent_id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listing_fingerprints_fp
    ON listing_fingerprints(fingerprint);
