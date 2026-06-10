-- 0083_action_mandates.sql
--
-- Phase 4 (the write web), fail-closed. An action mandate is a bounded,
-- revocable, expiring grant to perform ONE consequential web action under a
-- hard spend cap. Typed status + action_kind + reversibility make illegal
-- states unrepresentable. The whole feature is gated OFF by default
-- (AZTEA_ACTION_WEB_ENABLED) — a mandate row does nothing until ops opts in.

CREATE TABLE IF NOT EXISTS action_mandates (
    mandate_id          TEXT PRIMARY KEY,
    caller_owner_id     TEXT NOT NULL,
    agent_id            TEXT NOT NULL,                  -- the single agent allowed to act
    action_kind         TEXT NOT NULL
        CHECK (action_kind IN ('purchase','book','submit_form','cancel')),
    reversibility       TEXT NOT NULL
        CHECK (reversibility IN ('reversible','irreversible','unknown')),
    max_spend_cents     INTEGER NOT NULL CHECK (max_spend_cents >= 0),  -- real cost + fee ceiling
    currency            TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    allowed_domains     TEXT NOT NULL,                  -- JSON array, re-validated at use
    action_descriptor   TEXT NOT NULL,                  -- canonical JSON of the intended action
    status              TEXT NOT NULL DEFAULT 'issued'
        CHECK (status IN ('issued','authorized','consumed','revoked','expired')),
    confirmation_nonce  TEXT,                           -- single-use, cleared on consume
    mandate_sig         TEXT,
    mandate_sig_alg     TEXT,
    job_id              TEXT,                            -- one mandate maps to at most one job
    issued_at           TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    resolved_at         TEXT,
    resolution_note     TEXT
);

CREATE INDEX IF NOT EXISTS idx_action_mandates_caller ON action_mandates(caller_owner_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_mandates_status_expiry ON action_mandates(status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_action_mandates_job ON action_mandates(job_id) WHERE job_id IS NOT NULL;
