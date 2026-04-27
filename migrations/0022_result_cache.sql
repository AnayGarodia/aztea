CREATE TABLE IF NOT EXISTS agent_result_cache (
    cache_key     TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    output_json   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    job_id        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cache_agent ON agent_result_cache(agent_id);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON agent_result_cache(expires_at);
