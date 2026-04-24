-- 0013: password reset OTP tokens
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    expires_at   TEXT NOT NULL,
    used_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_prt_user ON password_reset_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_prt_hash ON password_reset_tokens(token_hash);
