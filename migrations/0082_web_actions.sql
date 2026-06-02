-- 0082_web_actions.sql
--
-- Phase 4. One row per attempted web action. commit_phase is the durable marker
-- that lets a sweeper fail-FORWARD: a worker that dies AFTER an irreversible
-- submit (commit_phase='submitted') must be reconciled/settled, never blindly
-- refunded (the caller already paid the merchant). The ledger (transactions) is
-- the money source of truth — the *_cents columns here are echoes for audit.

CREATE TABLE IF NOT EXISTS web_actions (
    action_id           TEXT PRIMARY KEY,
    job_id              TEXT,
    mandate_id          TEXT NOT NULL,
    phase               TEXT NOT NULL DEFAULT 'authorized'
        CHECK (phase IN ('authorized','previewed','awaiting_confirmation','executing','completed','failed')),
    commit_phase        TEXT NOT NULL DEFAULT 'pre_submit'
        CHECK (commit_phase IN ('pre_submit','submitted','settled')),
    target_domain       TEXT,
    quoted_cost_cents   INTEGER CHECK (quoted_cost_cents >= 0),
    actual_cost_cents   INTEGER CHECK (actual_cost_cents >= 0),
    agent_fee_cents     INTEGER NOT NULL DEFAULT 0 CHECK (agent_fee_cents >= 0),
    platform_fee_cents  INTEGER CHECK (platform_fee_cents >= 0),
    attestation_sig     TEXT,
    failure_code        TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    settled_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_web_actions_phase ON web_actions(phase, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_web_actions_mandate ON web_actions(mandate_id);
