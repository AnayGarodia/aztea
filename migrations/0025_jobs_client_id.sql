ALTER TABLE jobs ADD COLUMN client_id TEXT;
CREATE INDEX IF NOT EXISTS idx_jobs_client_created ON jobs(client_id, created_at DESC);
