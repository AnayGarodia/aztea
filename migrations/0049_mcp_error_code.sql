-- 0049_mcp_error_code.sql
-- Capture the structured error code on failed MCP invocations.
--
-- mcp_invocation_log.success answers "did this call work" but not "why did it
-- fail". The /admin/usage/query?view=failures view needs the latter — and
-- the value is already in the response envelope (error.code) that the MCP
-- dispatch path returns. Writing it costs nothing and turns a binary signal
-- into a queryable one.

ALTER TABLE mcp_invocation_log ADD COLUMN error_code TEXT;

CREATE INDEX IF NOT EXISTS idx_mcp_invocation_log_failure
    ON mcp_invocation_log(success, error_code, invoked_at DESC);
