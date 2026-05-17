-- Track per-run cumulative caller charges so /pipelines/<id>/runs/<run_id>
-- can return a top-level total_charged_cents rollup. Pre-0047 the MCP-side
-- session_spent_cents accumulator silently dropped every pipeline + recipe
-- run because the run response had no charge field for _charge_from_result
-- to read (the 2026-05-17 production audit, bug #6).
ALTER TABLE pipeline_runs ADD COLUMN total_charged_cents INTEGER NOT NULL DEFAULT 0;
