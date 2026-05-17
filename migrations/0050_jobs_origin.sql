-- 0050_jobs_origin.sql
-- Tag every job row with how it was created so spend can be attributed
-- per surface (manual direct call vs auto-hire vs pipeline step vs compare
-- replica vs recipe-driven vs watcher-triggered).
--
-- Allowed values (validated at insert site, not by CHECK constraint — the
-- canonical jobs table in core/jobs/db.py predates this column, and SQLite
-- ALTER TABLE handling of CHECK is inconsistent across versions):
--
--   direct     — POST /registry/agents/{id}/call or POST /jobs from a caller
--   auto_hire  — created via do_specialist_task / registry_auto_hire fast path
--   pipeline   — step inside a pipeline_runs execution
--   compare    — replica created by a compare_sessions run
--   recipe     — saved-pipeline template invocation
--   watcher    — triggered by watcher_runs (periodic monitor)
--
-- NULL is treated as "unknown / pre-migration" by readers. The backfill in
-- scripts/backfill_observability.py promotes NULL to 'direct' for rows that
-- have no pipeline/compare/recipe/watcher join match.

ALTER TABLE jobs ADD COLUMN origin TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_origin_created
    ON jobs(origin, created_at DESC);
