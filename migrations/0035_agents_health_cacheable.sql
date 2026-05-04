-- 0035_agents_health_cacheable.sql
-- Adds columns that exist in the canonical agents table definition in
-- core/registry/core_schema.py but were never captured as migrations.
-- These are no-ops on SQLite (handled by _ensure_* inline guards) but
-- are required for the PostgreSQL schema to match the application's
-- INSERT/SELECT column lists.

ALTER TABLE agents ADD COLUMN endpoint_health_status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE agents ADD COLUMN endpoint_consecutive_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN endpoint_last_checked_at TEXT;
ALTER TABLE agents ADD COLUMN endpoint_last_error TEXT;
ALTER TABLE agents ADD COLUMN cacheable INTEGER
