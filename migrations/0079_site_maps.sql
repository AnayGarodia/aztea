-- 0079_site_maps.sql
--
-- Phase 1 of the agent-readable-web build: the shared signed site-map commons.
-- One row per signed *version* of a site map for a normalized site_key. The
-- signed bytes (map_json + signature) are immutable, and the mutable reuse/health
-- counters live in side columns (same discipline as wallets.balance_cents being
-- a cache over the insert-only ledger). Competing maps for one site_key from
-- different authors coexist, and ranking picks the winner at read time.

CREATE TABLE IF NOT EXISTS site_maps (
    map_id                 TEXT PRIMARY KEY,        -- 'smap_' + 22 base62
    site_key               TEXT NOT NULL,           -- normalized host+path-pattern
    url_pattern            TEXT NOT NULL,           -- human-readable glob
    schema                 TEXT NOT NULL DEFAULT 'aztea/site-map/1',
    version                INTEGER NOT NULL DEFAULT 1,
    author_did             TEXT NOT NULL,           -- did:web of the contributing agent
    author_agent_id        TEXT NOT NULL,
    author_owner_id        TEXT NOT NULL,           -- payout wallet routing
    map_json               TEXT NOT NULL,           -- the signed affordance document (canonical JSON)
    map_sha256             TEXT NOT NULL,
    dom_fingerprint        TEXT NOT NULL,           -- value-stripped structural hash
    signature              TEXT NOT NULL,           -- Ed25519 (base64) over the manifest
    signature_alg          TEXT NOT NULL DEFAULT 'Ed25519+aztea-sitemap-sig/1',
    status                 TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','challenged','revoked','superseded')),
    hit_count              INTEGER NOT NULL DEFAULT 0,
    fresh_validation_count INTEGER NOT NULL DEFAULT 0,
    drift_count            INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL,
    last_used_at           TEXT,
    last_validated_at      TEXT,
    revoked_at             TEXT,
    revoked_reason         TEXT
);

CREATE INDEX IF NOT EXISTS idx_site_maps_lookup
    ON site_maps(site_key, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_maps_author
    ON site_maps(author_agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_maps_fingerprint
    ON site_maps(site_key, dom_fingerprint);
-- One active version per (site_key, author): a contributor refreshing a map
-- supersedes its own prior row rather than stacking duplicate active rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_site_maps_author_active
    ON site_maps(site_key, author_did) WHERE status = 'active';
