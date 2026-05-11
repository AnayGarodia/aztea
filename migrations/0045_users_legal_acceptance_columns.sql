-- 0045_users_legal_acceptance_columns.sql
-- Adds columns present in init_auth_db() inline DDL (core/auth/schema.py) but
-- never captured as migration files. Required for PostgreSQL where
-- init_auth_db() is a no-op (commit ed8ff3a) and migrations are the sole
-- schema source. Without these columns POST /auth/legal/accept raises
-- `column "terms_version_accepted" of relation "users" does not exist`,
-- surfacing to the user as "Internal server error."
--
-- The migration runner skips duplicate-column errors, so this is safe to
-- apply against SQLite databases where _ensure_users_schema() has already
-- added the columns.

ALTER TABLE users ADD COLUMN terms_version_accepted TEXT;
ALTER TABLE users ADD COLUMN privacy_version_accepted TEXT;
ALTER TABLE users ADD COLUMN legal_accepted_at TEXT;
ALTER TABLE users ADD COLUMN legal_accept_ip TEXT;
ALTER TABLE users ADD COLUMN legal_accept_user_agent TEXT;
