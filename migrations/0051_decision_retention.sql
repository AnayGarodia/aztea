-- 0051_decision_retention.sql
-- Documentary stub. The auto_hire_decisions table from 0047 will grow
-- unbounded under real traffic. The retention policy is:
--
--   1. Rows older than 90 days are aggregated to auto_hire_decisions_daily
--      (see migration 0051) so trend data survives the deletion.
--   2. After the aggregation succeeds, the raw rows are DELETEd in batches.
--   3. The rollup table is kept indefinitely.
--
-- The actual sweep runs from core.observability.run_decision_retention(),
-- which is invoked by the periodic-job loop in the FastAPI lifespan
-- (server/application_parts/part_006.py). This migration is a no-op so the
-- policy is recorded against a version number — future readers can grep
-- the migrations directory to find when retention started.

SELECT 1;  -- no-op
