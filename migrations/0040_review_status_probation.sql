-- 0040_review_status_probation.sql
--
-- Documentary migration: introduces 'probation' as a valid value of the
-- agents.review_status column. The column is plain TEXT with no CHECK
-- constraint, so this file performs no DDL — but it claims the migration
-- slot so the new enum value is traceable in `git log` and so future
-- schema-altering migrations remain correctly numbered.
--
-- See core/registry/core_schema.py:REVIEW_STATUSES for the runtime allowlist.

SELECT 1;  -- no-op
