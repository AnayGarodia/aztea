-- Add callback_url to jobs: orchestrator can register a push URL when hiring a specialist
ALTER TABLE jobs ADD COLUMN callback_url TEXT;
