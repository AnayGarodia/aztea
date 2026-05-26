-- 0066_pbkdf2_iterations.sql
-- Per-user PBKDF2 iteration count so we can raise the default from 100k to
-- 600k without invalidating historical hashes. Login verifies with the stored
-- iteration count. When that count is below the current default the next
-- successful login rehashes the password at the new cost. New rows inserted
-- by core.auth.users.register_user always write the current default.
-- Legacy rows get the historical floor (100k) so existing logins keep
-- verifying. Future bumps follow the same pattern, bumping PBKDF2_ITERATIONS
-- in core/auth/schema.py and leaving this column alone.
ALTER TABLE users ADD COLUMN pbkdf2_iterations INTEGER NOT NULL DEFAULT 100000;

-- Mirror the column on the pending-signup token table so a deploy that bumps
-- the iteration count mid-OTP-window (10-minute TTL) does not strand users
-- with a hash that no longer matches the new default. The column is written
-- by issue_signup_verification and propagated to users at consume.
ALTER TABLE signup_verification_tokens
    ADD COLUMN pbkdf2_iterations INTEGER NOT NULL DEFAULT 100000;
