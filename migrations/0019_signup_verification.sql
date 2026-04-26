-- Pending signup verifications: holds the hashed registration payload
-- until the email-OTP is consumed. We don't create the user row until
-- /auth/verify-signup succeeds, so unverified emails never become accounts.
CREATE TABLE IF NOT EXISTS signup_verification_tokens (
    token_id     TEXT PRIMARY KEY,
    email        TEXT NOT NULL,
    username     TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt         TEXT NOT NULL,
    role         TEXT NOT NULL,
    code_hash    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    consumed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_signup_tokens_email
    ON signup_verification_tokens (LOWER(email));

CREATE INDEX IF NOT EXISTS idx_signup_tokens_active
    ON signup_verification_tokens (expires_at)
    WHERE consumed_at IS NULL;
