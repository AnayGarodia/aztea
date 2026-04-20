-- 0007_jobs_tree_depth.sql
-- Persist orchestration lineage depth for max-depth enforcement.

ALTER TABLE jobs
ADD COLUMN tree_depth INTEGER NOT NULL DEFAULT 0 CHECK(tree_depth >= 0);
