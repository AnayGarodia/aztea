-- Tracks why an agent was suspended so health-check suspensions can be auto-reinstated.
ALTER TABLE agents ADD COLUMN suspension_reason TEXT;
