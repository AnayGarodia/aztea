-- Phase 0: performance infrastructure
-- Adds an index on the result cache expiry column (makes cache pruning fast)
-- and a new ring-buffer metrics table for call timing.

CREATE INDEX IF NOT EXISTS idx_cache_expires
    ON agent_result_cache(expires_at);

CREATE TABLE IF NOT EXISTS tool_invocation_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT    NOT NULL,
    caller_id   TEXT,
    latency_ms  REAL    NOT NULL,
    bytes_in    INTEGER NOT NULL DEFAULT 0,
    bytes_out   INTEGER NOT NULL DEFAULT 0,
    cached      INTEGER NOT NULL DEFAULT 0,  -- 1 = served from cache
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_agent
    ON tool_invocation_metrics(agent_id, created_at);
