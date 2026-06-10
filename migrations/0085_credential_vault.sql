-- 0085_credential_vault.sql
--
-- Phase 4 (the write web), fail-closed. Encrypted store of a user's website
-- logins so web_actor can act on the user's existing accounts. The whole feature
-- is gated OFF by default (AZTEA_CREDENTIAL_VAULT_ENABLED) and requires a
-- configured KEK provider (hosted KMS, or an explicitly opted-in local KEK). With
-- neither, the vault refuses to store rather than store weakly.
--
-- INVARIANT: there is NO plaintext column. Secret material lives ONLY as
-- envelope-encrypted ciphertext (AES-256-GCM). The data-encryption key (DEK) is
-- itself wrapped by a key (the KEK) that never touches this table. Metadata is
-- queryable but secrets are not, so a DB dump alone yields nothing without the KEK.

CREATE TABLE IF NOT EXISTS website_credentials (
    credential_id     TEXT PRIMARY KEY,            -- 'cred_' + 22-char base62
    owner_id          TEXT NOT NULL,               -- user_id, the scoping key
    domain            TEXT NOT NULL,               -- registrable host, normalized lowercase
    label             TEXT,                        -- human label ("my Amazon"), non-secret
    cred_kind         TEXT NOT NULL
        CHECK (cred_kind IN ('password','totp','cookies')),
    enc_scheme        TEXT NOT NULL,               -- 'AESGCM+kms-dek/1' or 'AESGCM+local-dek/1'
    wrapped_dek       BLOB NOT NULL,               -- DEK wrapped by the KEK (KMS ciphertext or local-wrapped)
    kek_ref           TEXT NOT NULL,               -- KMS key id/arn OR 'local:<key_fingerprint>'
    nonce             BLOB NOT NULL,               -- 12-byte AES-GCM nonce, unique per row
    ciphertext        BLOB NOT NULL,               -- AES-GCM(plaintext_json, dek, nonce, aad)
    aad_fingerprint   TEXT NOT NULL,               -- sha256 of the AAD bound into the ciphertext
    status            TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','revoked','rotating')),
    version           INTEGER NOT NULL DEFAULT 1,  -- bumped on rotate
    last_used_at      TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    revoked_at        TEXT
);

-- One active credential per (owner, domain, kind). Revoked rows do not block re-add.
CREATE UNIQUE INDEX IF NOT EXISTS uq_website_credentials_scope
    ON website_credentials(owner_id, domain, cred_kind) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_website_credentials_owner
    ON website_credentials(owner_id, domain);
