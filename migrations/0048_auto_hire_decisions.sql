-- 0048_auto_hire_decisions.sql
-- Persisted audit of every do_specialist_task / auto-hire decision.
--
-- Without this, gating outcomes ("no_match", "insufficient_confidence",
-- "price_exceeded", "insufficient_trust", ...) live only in the HTTP response
-- body and are unrecoverable for trend analysis. That blocks three product
-- questions: which intents fail to match, the gated-vs-auto-invoked ratio,
-- and the dry_run -> real_call conversion rate.
--
-- Insert path: core/registry/decision_audit.record_decision(), called at the
-- end of registry_auto_hire() in server/application_parts/part_012.py.
-- Fire-and-forget: any write failure is logged but never blocks the response.
--
-- Retention: 90 days of raw rows. Older rows are aggregated to
-- auto_hire_decisions_daily (see migration 0051) before deletion. See the
-- sweeper job in core/observability.py and migration 0050 for the policy
-- stub.

CREATE TABLE IF NOT EXISTS auto_hire_decisions (
    decision_id        TEXT PRIMARY KEY,
    caller_owner_id    TEXT,
    caller_key_id      TEXT,
    intent_text        TEXT NOT NULL,
    intent_hash        TEXT NOT NULL,
    auto_invoked       INTEGER NOT NULL DEFAULT 0 CHECK(auto_invoked IN (0,1)),
    dry_run            INTEGER NOT NULL DEFAULT 0 CHECK(dry_run IN (0,1)),
    reason             TEXT,
    chosen_agent_id    TEXT,
    confidence         REAL,
    candidates_json    TEXT,
    resulting_job_id   TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auto_hire_decisions_created
    ON auto_hire_decisions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auto_hire_decisions_reason
    ON auto_hire_decisions(reason, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auto_hire_decisions_intent_hash
    ON auto_hire_decisions(intent_hash);
CREATE INDEX IF NOT EXISTS idx_auto_hire_decisions_chosen
    ON auto_hire_decisions(chosen_agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auto_hire_decisions_caller
    ON auto_hire_decisions(caller_owner_id, created_at DESC);
