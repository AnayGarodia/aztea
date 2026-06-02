-- 0079_site_map_usages_challenges.sql
--
-- site_map_usages: insert-only log of every reuse, and the idempotency anchor
-- for royalty payouts. UNIQUE(consumer_job_id) means a retried job can never
-- double-pay the map author (plan amendment A4 — settle off this row, never the
-- ledger related_tx_id, which collides with post_call_payout).
--
-- site_map_challenges: the cache-poisoning defense, made auditable. A consumer
-- who replays a map and gets wrong data files a bonded challenge that can
-- escalate into the existing two-judge dispute system.

CREATE TABLE IF NOT EXISTS site_map_usages (
    usage_id            TEXT PRIMARY KEY,           -- 'smu_' + 22 base62
    map_id              TEXT,                        -- nullable: usage may be of an api_spec only
    api_spec_id         TEXT,
    site_key            TEXT NOT NULL,
    consumer_job_id     TEXT NOT NULL,
    consumer_owner_id   TEXT NOT NULL,
    author_owner_id     TEXT NOT NULL,
    royalty_cents       INTEGER NOT NULL DEFAULT 0 CHECK (royalty_cents >= 0),
    royalty_tx_id       TEXT,                        -- ledger payout tx (NULL until settled)
    validated_fresh     INTEGER NOT NULL DEFAULT 0,  -- 1 = fingerprint matched, 0 = drift fallback
    created_at          TEXT NOT NULL
);

-- Idempotency: at most one usage row per consuming job.
CREATE UNIQUE INDEX IF NOT EXISTS uq_site_map_usages_job
    ON site_map_usages(consumer_job_id);
CREATE INDEX IF NOT EXISTS idx_site_map_usages_map
    ON site_map_usages(map_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_map_usages_author
    ON site_map_usages(author_owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS site_map_challenges (
    challenge_id        TEXT PRIMARY KEY,           -- 'smc_' + 22 base62
    map_id              TEXT,
    api_spec_id         TEXT,
    site_key            TEXT NOT NULL,
    challenger_owner_id TEXT NOT NULL,
    challenger_job_id   TEXT,
    reason              TEXT NOT NULL
        CHECK (reason IN ('wrong_data','selectors_missing','poisoned','stale','ssrf_redirect','other')),
    evidence_json       TEXT,
    bond_cents          INTEGER NOT NULL DEFAULT 0 CHECK (bond_cents >= 0),
    bond_tx_id          TEXT,
    dispute_id          TEXT,                        -- bridge into the two-judge dispute system
    status              TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','upheld','rejected','auto_revoked')),
    created_at          TEXT NOT NULL,
    resolved_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_site_map_challenges_map
    ON site_map_challenges(map_id, status);
CREATE INDEX IF NOT EXISTS idx_site_map_challenges_open
    ON site_map_challenges(status, created_at DESC);
