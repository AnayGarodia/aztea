-- 0056_latency_decay.sql
-- Surgical band-aid for stale agent latency averages.
--
-- Background: agents.avg_latency_ms is a lifetime running average updated
-- in core/registry/agents_ops.update_call_stats. A single old very-slow
-- call (e.g. a 181-second sync timeout) takes thousands of fast follow-ups
-- to dilute, so the catalog kept showing "181 s average" for agents that
-- ran in 0.3 s on every recent invocation. The proper fix is a rolling-
-- window or weighted-recent average. This migration just unblocks the UI
-- damage by tracking when latency was last decayed so a daily sweep can
-- pull stale values toward zero. See _apply_latency_decay in part_005.py.

ALTER TABLE agents ADD COLUMN latency_last_decay_at TEXT;
