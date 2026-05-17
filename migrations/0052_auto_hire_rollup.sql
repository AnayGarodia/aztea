-- 0052_auto_hire_rollup.sql
-- Daily aggregate of auto_hire_decisions so trends past the 90-day raw
-- retention window remain queryable, just at lower granularity.
--
-- One row per (day, reason). intent_hashes stores up to N representative
-- intent_hash values per bucket as a JSON array — enough to surface "the
-- same no_match query keeps coming back" without preserving free-form text
-- past the retention deadline.

CREATE TABLE IF NOT EXISTS auto_hire_decisions_daily (
    day                TEXT NOT NULL,
    reason             TEXT,
    auto_invoked       INTEGER NOT NULL DEFAULT 0 CHECK(auto_invoked IN (0,1)),
    decision_count     INTEGER NOT NULL DEFAULT 0 CHECK(decision_count >= 0),
    unique_callers     INTEGER NOT NULL DEFAULT 0 CHECK(unique_callers >= 0),
    intent_hashes      TEXT,
    rolled_up_at       TEXT NOT NULL,
    PRIMARY KEY (day, reason, auto_invoked)
);

CREATE INDEX IF NOT EXISTS idx_auto_hire_daily_day
    ON auto_hire_decisions_daily(day DESC);
CREATE INDEX IF NOT EXISTS idx_auto_hire_daily_reason
    ON auto_hire_decisions_daily(reason, day DESC);
