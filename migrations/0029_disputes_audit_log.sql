-- Adds audit_log column to disputes for human-readable history of state changes.
-- Used by the admin rule route and judge flow to append structured event entries.
ALTER TABLE disputes ADD COLUMN audit_log TEXT DEFAULT '[]';
