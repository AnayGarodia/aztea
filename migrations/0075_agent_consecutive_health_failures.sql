-- 0070_agent_consecutive_health_failures.sql
--
-- Counter for the continuous endpoint-health sweeper introduced in Plan B
-- Phase 3b. Increments on every consecutive failed probe, resets to 0 on
-- a successful probe. When the counter crosses
-- AZTEA_HEALTH_SUSPEND_THRESHOLD (default 3), the sweeper transitions
-- the agent to status=suspended with suspension_reason=health_check_failed
-- so buyers stop being charged for a dead endpoint.
--
-- last_health_status (migration 0008) and endpoint_health_status (0035)
-- already exist. This adds the count so the suspend threshold doesn't
-- depend on a stateful read of the boolean status alone.

ALTER TABLE agents ADD COLUMN consecutive_health_failures INTEGER NOT NULL DEFAULT 0;
