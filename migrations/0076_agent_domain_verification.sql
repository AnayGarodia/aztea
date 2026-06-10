-- 0076_agent_domain_verification.sql
--
-- Optional domain-ownership badge introduced in Plan B Phase 3c. Sellers
-- can prove they control the domain hosting their endpoint by serving a
-- well-known JSON file OR setting a DNS TXT record. Verification lifts
-- buyer trust without making it mandatory — non-verified agents still
-- list normally, they just don't get the badge or the small auto-hire
-- ranking bonus.

ALTER TABLE agents ADD COLUMN domain_verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN domain_verified_at TEXT;
ALTER TABLE agents ADD COLUMN domain_verification_method TEXT;
