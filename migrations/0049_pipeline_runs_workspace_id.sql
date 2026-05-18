-- 0049_pipeline_runs_workspace_id.sql
-- Link pipeline runs to their auto-created workspace.
--
-- When a recipe definition opts in via ``auto_workspace: true``, the
-- pipeline executor creates a workspace at run-start, threads its ID
-- through every step's dispatch envelope, and seals it on successful
-- completion. The workspace_id column lets the run-status API surface
-- that link so callers can fetch the sealed manifest.
--
-- Nullable: pre-existing runs and recipes that don't opt in have
-- workspace_id = NULL.

ALTER TABLE pipeline_runs ADD COLUMN workspace_id TEXT NULL;
