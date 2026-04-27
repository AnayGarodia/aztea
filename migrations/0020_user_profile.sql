-- Profile + billing fields on the user row.
-- phone and full_name back the new Account section in Settings.
-- stripe_customer_id is created lazily on the first SetupIntent so saved
-- cards survive across top-up sessions.
ALTER TABLE users ADD COLUMN phone TEXT;
ALTER TABLE users ADD COLUMN full_name TEXT;
ALTER TABLE users ADD COLUMN stripe_customer_id TEXT;

CREATE INDEX IF NOT EXISTS idx_users_stripe_customer
    ON users (stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;
