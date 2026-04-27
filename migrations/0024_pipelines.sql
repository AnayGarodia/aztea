CREATE TABLE IF NOT EXISTS pipelines (
    pipeline_id   TEXT PRIMARY KEY,
    owner_id      TEXT NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    definition    TEXT NOT NULL,
    is_public     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          TEXT PRIMARY KEY,
    pipeline_id     TEXT NOT NULL REFERENCES pipelines(pipeline_id),
    caller_owner_id TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    input_json      TEXT NOT NULL,
    output_json     TEXT,
    error_message   TEXT,
    step_results    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipelines_owner_updated
ON pipelines(owner_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipelines_public_updated
ON pipelines(is_public, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_pipeline_created
ON pipeline_runs(pipeline_id, created_at DESC);
