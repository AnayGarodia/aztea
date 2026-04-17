-- Add LLM provider and model ID columns to agents table
ALTER TABLE agents ADD COLUMN model_provider TEXT;
ALTER TABLE agents ADD COLUMN model_id TEXT;
CREATE INDEX IF NOT EXISTS idx_agents_model_provider ON agents(model_provider);
