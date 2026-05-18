-- 0053_workspaces.sql
-- Server-side shared-state primitive for multi-agent workflows.
--
-- A workspace is a named collection of artifacts (named blobs) that
-- multiple agents in one workflow read from and write to, instead of
-- threading payloads through the calling agent's context. Workspaces
-- can be sealed: a signed Ed25519 manifest over all artifact hashes
-- becomes verifiable evidence of the whole workflow.
--
-- Storage: BLOB inline (v0). The schema reserves external_store_uri
-- so a future S3-backed mode is an additive migration, not a rewrite.
-- Backing: 'bytea' (default) stores content inline. 'sandbox' routes
-- reads/writes through core/sandbox/filesystem.py against backing_id.
--
-- Lifecycle: active --> sealed --> expired (content nulled by sweeper),
-- or active --> sandbox_evicted (terminal, sandbox died mid-workflow).

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id        TEXT PRIMARY KEY,
    owner_user_id       TEXT NOT NULL,
    run_id              TEXT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'sealed', 'expired', 'sandbox_evicted')),
    backing_type        TEXT NOT NULL DEFAULT 'bytea'
        CHECK (backing_type IN ('bytea', 'sandbox')),
    backing_id          TEXT NULL,
    external_store_uri  TEXT NULL,
    total_bytes         INTEGER NOT NULL DEFAULT 0,
    artifact_count      INTEGER NOT NULL DEFAULT 0,
    quota_bytes         INTEGER NOT NULL DEFAULT 67108864,
    seal_manifest       TEXT NULL,
    seal_signature      TEXT NULL,
    seal_public_key_did TEXT NULL,
    created_at          TEXT NOT NULL,
    sealed_at           TEXT NULL,
    expires_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS workspaces_owner_idx
    ON workspaces(owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS workspaces_run_idx
    ON workspaces(run_id);

CREATE INDEX IF NOT EXISTS workspaces_sweeper_idx
    ON workspaces(status, expires_at);

CREATE TABLE IF NOT EXISTS workspace_artifacts (
    artifact_id          TEXT PRIMARY KEY,
    workspace_id         TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    name                 TEXT NOT NULL,
    content_type         TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes           INTEGER NOT NULL,
    sha256               TEXT NOT NULL,
    content              BLOB NULL,
    created_by_agent_id  TEXT NULL,
    created_by_job_id    TEXT NULL,
    created_at           TEXT NOT NULL,
    UNIQUE (workspace_id, name)
);

CREATE INDEX IF NOT EXISTS workspace_artifacts_workspace_idx
    ON workspace_artifacts(workspace_id, created_at);
