-- 0067_hosted_execution_log.sql
-- Persisted audit of every hosted-skill / playground-test execution.
--
-- Without this we cannot answer how often the sandbox kills a runaway
-- payload, what the typical CPU and memory footprint of a published agent
-- is, which agents drift toward the resource ceiling and warrant operator
-- attention, and -- critically -- how often we kill on a security signal
-- (timeout, OOM, signal). The Wave 3 platform pivot opens hosted code
-- execution to anonymous /api/playground/test traffic, so we need the
-- evidence trail before the surface goes live.
--
-- Insert path lives in core/hosted_execution_log.record_execution.
-- Fire-and-forget. A write failure is logged but never blocks the response.
--
-- Retention is indefinite for now. Aggregate and prune in a follow-up once
-- volume warrants it (same pattern as auto_hire_decisions / 0051).

CREATE TABLE IF NOT EXISTS hosted_execution_log (
    execution_id        TEXT PRIMARY KEY,
    -- Where the call came from. 'playground_test' or 'hosted_skill_call'.
    -- Distinguishes anonymous probe traffic from real billed invocations
    -- so we can compute kill-rates per surface separately.
    surface             TEXT NOT NULL,
    -- Caller identity. NULL for anonymous /api/playground/test calls.
    caller_owner_id     TEXT,
    caller_key_id       TEXT,
    -- The hosted_skill_id when this was a registered agent invocation,
    -- or NULL for the ad-hoc /api/playground/test path.
    skill_id            TEXT,
    -- Hashes only. We never persist the raw input/output. Hashes let
    -- abuse investigators correlate repeated identical probes without
    -- retaining PII or buyer code.
    input_hash          TEXT,
    output_hash         TEXT,
    -- Resource accounting. peak_memory_mb / cpu_seconds may be NULL on
    -- backends that do not surface them (python_executor reports
    -- execution_time_ms only, while the Docker sandbox reports the full set).
    execution_time_ms   INTEGER NOT NULL,
    peak_memory_mb      REAL,
    cpu_seconds         REAL,
    sandbox_exit_code   INTEGER NOT NULL,
    -- 0 means ran to completion. Otherwise kill_reason is one of
    -- 'timeout' / 'oom' / 'signal' / 'sandbox_block'. NULL when was_killed = 0.
    was_killed          INTEGER NOT NULL DEFAULT 0 CHECK(was_killed IN (0,1)),
    kill_reason         TEXT,
    -- Free-form structured payload for fields we add later (network
    -- egress hits, audit-hook hits, etc.) without a new migration.
    extra_json          TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hosted_exec_log_created
    ON hosted_execution_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hosted_exec_log_skill
    ON hosted_execution_log(skill_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hosted_exec_log_caller
    ON hosted_execution_log(caller_owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hosted_exec_log_killed
    ON hosted_execution_log(was_killed, kill_reason, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hosted_exec_log_surface
    ON hosted_execution_log(surface, created_at DESC);
