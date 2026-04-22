-- Audit log for /mcp/invoke calls.
-- input_hash is SHA-256 of the raw input JSON (full input/output is never stored).
CREATE TABLE IF NOT EXISTS mcp_invocation_log (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    caller_key_id TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    input_hash    TEXT NOT NULL,
    invoked_at    TEXT NOT NULL,
    duration_ms   INTEGER,
    success       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mcp_log_agent    ON mcp_invocation_log(agent_id, invoked_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_log_caller   ON mcp_invocation_log(caller_key_id, invoked_at DESC);
