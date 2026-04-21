-- Add agent health check tracking columns
ALTER TABLE agents ADD COLUMN last_health_check_at TEXT;
ALTER TABLE agents ADD COLUMN last_health_status TEXT NOT NULL DEFAULT 'unknown';
