-- 0058_dispute_operator_response.sql
-- Add an operator-response slot to the dispute lifecycle so the agent
-- operator (not just the caller) has a chance to defend their work
-- before the LLM judges decide. The 2026-05-18 test report flagged this
-- as a substrate gap: callers could file disputes and operators had no
-- recourse path of their own.
--
-- New columns are NULL-able so pre-migration disputes stay valid.
--
-- The lifecycle gains a new status, 'awaiting_operator'. Sequence:
--   pending -> (caller files)
--     -> awaiting_operator (the operator gets a response window)
--       -> judging (operator responded OR window expired)
--         -> consensus / tied
--           -> resolved / final / appealed
--
-- The CHECK constraint on disputes.status was set at table-creation
-- time and cannot be ALTERed in SQLite without a table rewrite, so the
-- enforcement is done in core/disputes.py at the application layer.

ALTER TABLE disputes ADD COLUMN operator_response_text TEXT;
ALTER TABLE disputes ADD COLUMN operator_response_at TEXT;
ALTER TABLE disputes ADD COLUMN operator_response_deadline TEXT;

CREATE INDEX IF NOT EXISTS disputes_operator_deadline_idx
    ON disputes(operator_response_deadline)
    WHERE status = 'awaiting_operator';
