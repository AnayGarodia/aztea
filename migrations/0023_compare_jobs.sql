CREATE TABLE IF NOT EXISTS compare_sessions (
    compare_id      TEXT PRIMARY KEY,
    caller_owner_id TEXT NOT NULL,
    input_json      TEXT NOT NULL,
    agent_ids_json  TEXT NOT NULL,
    job_ids_json    TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'running',
    winner_agent_id TEXT,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_compare_owner_created
ON compare_sessions(caller_owner_id, created_at DESC);
