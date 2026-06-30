-- grant_admin.sql — grant admin scope to specific users' active session keys.
--
-- The web dashboard (/admin/otto, etc.) is gated on the session key carrying the
-- "admin" scope. Login reuses the existing active "Session key" row, so setting
-- its scopes here sticks across re-logins.
--
-- Run on the prod box:  sudo -u aztea psql "$DATABASE_URL" -f scripts/grant_admin.sql
-- (or paste into psql). Idempotent — safe to re-run.

BEGIN;

UPDATE api_keys
SET scopes = '["caller","worker","admin"]'
WHERE name = 'Session key'
  AND is_active = 1
  AND user_id IN (
      SELECT user_id FROM users
      WHERE lower(email) IN (
          'ag5250@columbia.edu',
          'ps35612@columbia.edu',
          'agarodia98@gmail.com'
      )
  );

-- Show the result so you can confirm each account was updated. A user who has
-- never logged in has no Session key yet → 0 rows for them (have them log in
-- once, then re-run this).
SELECT u.email, k.key_prefix, k.scopes, k.is_active
FROM users u
JOIN api_keys k ON k.user_id = u.user_id AND k.name = 'Session key'
WHERE lower(u.email) IN (
    'ag5250@columbia.edu',
    'ps35612@columbia.edu',
    'agarodia98@gmail.com'
)
ORDER BY u.email, k.created_at DESC;

COMMIT;
