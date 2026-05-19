-- 2026-05-19 (B14): pin dispute resolution_by deadline at filing time.
--
-- Pre-fix, resolution_by was either computed on the fly each time the
-- response was rendered (filed_at + sliding ETA) or, on some surfaces,
-- shifted when a judge run touched the row. Either way the deadline a
-- caller saw at filing did not equal the deadline they saw an hour
-- later, which made resolution_by useless as a client-side scheduling
-- signal.
--
-- New behavior: resolution_deadline_at is set ONCE at INSERT to
-- filed_at + DEFAULT_DISPUTE_RESOLUTION_HOURS (48h) and is never
-- UPDATEd. The response builder reads it back as `resolution_by`.
--
-- Migration is bare ADD COLUMN per the existing repo style. Migration
-- comments stay free of semicolons to avoid the _split_statements
-- naive splitter (B-fix from 2026-05-19 cluster A).

ALTER TABLE disputes ADD COLUMN resolution_deadline_at TEXT;
