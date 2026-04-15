-- 0001_initial.sql
-- Initial schema for agentmarket: all tables from core modules and server ops.

-- ── Auth ──────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','banned'))
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id        TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,
    key_prefix    TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT 'Default',
    scopes        TEXT NOT NULL DEFAULT '["caller","worker"]',
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS agent_keys (
    key_id      TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    key_hash    TEXT NOT NULL UNIQUE,
    key_prefix  TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT 'Agent key',
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user_active ON api_keys(user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_agent_keys_agent_active ON agent_keys(agent_id, revoked_at);

-- ── Registry ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agents (
    agent_id            TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL,
    name                TEXT NOT NULL UNIQUE,
    description         TEXT NOT NULL,
    endpoint_url        TEXT NOT NULL,
    price_per_call_usd  REAL NOT NULL CHECK(price_per_call_usd >= 0),
    avg_latency_ms      REAL NOT NULL DEFAULT 0.0,
    total_calls         INTEGER NOT NULL DEFAULT 0,
    successful_calls    INTEGER NOT NULL DEFAULT 0,
    tags                TEXT NOT NULL DEFAULT '[]',
    input_schema        TEXT NOT NULL DEFAULT '{}',
    output_schema       TEXT NOT NULL DEFAULT '{}',
    output_verifier_url TEXT,
    internal_only       INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','banned')),
    trust_decay_multiplier REAL NOT NULL DEFAULT 1.0,
    last_decay_at       TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_embeddings (
    agent_id     TEXT PRIMARY KEY REFERENCES agents(agent_id) ON DELETE CASCADE,
    embedding    BLOB NOT NULL,
    source_text  TEXT NOT NULL,
    embedded_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_name ON agents(name);
CREATE INDEX IF NOT EXISTS idx_agents_created ON agents(created_at);
CREATE INDEX IF NOT EXISTS idx_agent_embeddings_embedded_at ON agent_embeddings(embedded_at DESC);

-- ── Payments ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS wallets (
    wallet_id     TEXT PRIMARY KEY,
    owner_id      TEXT NOT NULL UNIQUE,
    balance_cents INTEGER NOT NULL DEFAULT 0 CHECK(balance_cents >= 0),
    caller_trust  REAL NOT NULL DEFAULT 0.5,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_id         TEXT PRIMARY KEY,
    wallet_id     TEXT NOT NULL,
    type          TEXT NOT NULL CHECK(type IN ('deposit','charge','fee','refund','payout')),
    amount_cents  INTEGER NOT NULL,
    related_tx_id TEXT,
    agent_id      TEXT,
    memo          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id          TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    invariant_ok    INTEGER NOT NULL,
    drift_cents     INTEGER NOT NULL,
    mismatch_count  INTEGER NOT NULL,
    summary_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS caller_trust_events (
    event_id      TEXT PRIMARY KEY,
    owner_id      TEXT NOT NULL,
    delta         REAL NOT NULL,
    before_value  REAL NOT NULL,
    after_value   REAL NOT NULL,
    reason        TEXT NOT NULL,
    related_id    TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_wallet ON transactions(wallet_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_owner ON wallets(owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_related_unique
    ON transactions(related_tx_id, type, wallet_id)
    WHERE related_tx_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_recon_created ON reconciliation_runs(created_at DESC);

-- ── Jobs ──────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS jobs (
    job_id              TEXT PRIMARY KEY,
    agent_id            TEXT NOT NULL,
    agent_owner_id      TEXT NOT NULL,
    caller_owner_id     TEXT NOT NULL,
    caller_wallet_id    TEXT NOT NULL,
    agent_wallet_id     TEXT NOT NULL,
    platform_wallet_id  TEXT NOT NULL,
    status              TEXT NOT NULL,
    price_cents         INTEGER NOT NULL CHECK(price_cents >= 0),
    charge_tx_id        TEXT NOT NULL,
    input_payload       TEXT NOT NULL,
    output_payload      TEXT,
    error_message       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    completed_at        TEXT,
    settled_at          TEXT,
    claim_owner_id      TEXT,
    claim_token         TEXT,
    claimed_at          TEXT,
    lease_expires_at    TEXT,
    last_heartbeat_at   TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    max_attempts        INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts >= 1),
    retry_count         INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    next_retry_at       TEXT,
    last_retry_at       TEXT,
    timeout_count       INTEGER NOT NULL DEFAULT 0 CHECK(timeout_count >= 0),
    last_timeout_at     TEXT,
    dispute_window_hours INTEGER NOT NULL DEFAULT 72 CHECK(dispute_window_hours >= 1),
    dispute_outcome      TEXT,
    judge_agent_id       TEXT,
    judge_verdict        TEXT,
    quality_score        INTEGER
);

CREATE TABLE IF NOT EXISTS job_messages (
    message_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT NOT NULL,
    from_id        TEXT NOT NULL,
    type           TEXT NOT NULL,
    payload        TEXT NOT NULL,
    correlation_id TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_caller ON jobs(caller_owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_agent ON jobs(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_agent_owner ON jobs(agent_owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_retry_due ON jobs(next_retry_at, status);
CREATE INDEX IF NOT EXISTS idx_jobs_lease_due ON jobs(lease_expires_at, status);
CREATE INDEX IF NOT EXISTS idx_job_messages_job ON job_messages(job_id, message_id ASC);

-- ── Disputes ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS disputes (
    dispute_id           TEXT PRIMARY KEY,
    job_id               TEXT NOT NULL REFERENCES jobs(job_id),
    filed_by_owner_id    TEXT NOT NULL,
    side                 TEXT NOT NULL CHECK(side IN ('caller','agent')),
    reason               TEXT NOT NULL,
    evidence             TEXT,
    status               TEXT NOT NULL CHECK(status IN ('pending','judging','consensus','tied','resolved','appealed','final')),
    outcome              TEXT CHECK(outcome IN ('caller_wins','agent_wins','split','void')),
    split_caller_cents   INTEGER,
    split_agent_cents    INTEGER,
    filed_at             TEXT NOT NULL,
    resolved_at          TEXT
);

CREATE TABLE IF NOT EXISTS dispute_judgments (
    judgment_id     TEXT PRIMARY KEY,
    dispute_id      TEXT NOT NULL,
    judge_kind      TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    reasoning       TEXT NOT NULL,
    model           TEXT,
    admin_user_id   TEXT,
    created_at      TEXT NOT NULL
);

-- caller_ratings is shared between disputes.py and reputation.py
CREATE TABLE IF NOT EXISTS caller_ratings (
    rating_id         TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL UNIQUE,
    caller_owner_id   TEXT NOT NULL,
    agent_owner_id    TEXT NOT NULL,
    rating            INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    comment           TEXT,
    created_at        TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_disputes_job_unique ON disputes(job_id);
CREATE INDEX IF NOT EXISTS idx_disputes_status_filed ON disputes(status, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_disputes_filer_filed ON disputes(filed_by_owner_id, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispute_judgments_dispute_created ON dispute_judgments(dispute_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_caller_ratings_caller_created ON caller_ratings(caller_owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_caller_ratings_agent_created ON caller_ratings(agent_owner_id, created_at DESC);

-- ── Reputation ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS job_quality_ratings (
    rating_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT NOT NULL UNIQUE,
    agent_id         TEXT NOT NULL,
    caller_owner_id  TEXT NOT NULL,
    rating           INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_quality_agent ON job_quality_ratings(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_quality_caller ON job_quality_ratings(caller_owner_id, created_at DESC);

-- ── Ops / Events / Hooks ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS job_events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            TEXT NOT NULL,
    agent_id          TEXT NOT NULL,
    agent_owner_id    TEXT NOT NULL,
    caller_owner_id   TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    actor_owner_id    TEXT,
    payload           TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_event_hooks (
    hook_id            TEXT PRIMARY KEY,
    owner_id           TEXT NOT NULL,
    target_url         TEXT NOT NULL,
    secret             TEXT,
    is_active          INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    last_attempt_at    TEXT,
    last_success_at    TEXT,
    last_status_code   INTEGER,
    last_error         TEXT
);

CREATE TABLE IF NOT EXISTS job_event_deliveries (
    delivery_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id            INTEGER NOT NULL,
    hook_id             TEXT NOT NULL,
    owner_id            TEXT NOT NULL,
    target_url          TEXT NOT NULL,
    secret              TEXT,
    payload             TEXT NOT NULL,
    status              TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'failed', 'cancelled')),
    attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    next_attempt_at     TEXT NOT NULL,
    last_attempt_at     TEXT,
    last_success_at     TEXT,
    last_status_code    INTEGER,
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(event_id, hook_id)
);

CREATE INDEX IF NOT EXISTS idx_job_events_owner_created ON job_events(caller_owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_events_agent_owner_created ON job_events(agent_owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_events_job_created ON job_events(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_hooks_owner_active ON job_event_hooks(owner_id, is_active);
CREATE INDEX IF NOT EXISTS idx_job_event_deliveries_status_due
    ON job_event_deliveries(status, next_attempt_at, delivery_id);
CREATE INDEX IF NOT EXISTS idx_job_event_deliveries_owner_created
    ON job_event_deliveries(owner_id, created_at DESC);
