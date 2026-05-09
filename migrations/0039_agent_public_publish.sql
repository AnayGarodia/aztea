-- 0039_agent_public_publish.sql
--
-- Adds a column tracking whether an agent has been syndicated to aztea.ai's
-- public registry, and when. Local-only agents have NULL here. The hosted
-- service updates this when /registry/agents/{id}/publish is called against
-- this instance and forwards the spec to the hosted public catalog.
--
-- This is metadata only — no behavior changes for OSS-mode users. The
-- column exists in every install so the publish route can read it without
-- a feature-flag-gated migration.

ALTER TABLE agents ADD COLUMN published_to_public_at TEXT;
ALTER TABLE agents ADD COLUMN published_to_public_listing_id TEXT;
