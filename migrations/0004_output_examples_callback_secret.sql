-- output_examples: JSON array of {input, output} pairs for agent discovery
ALTER TABLE agents ADD COLUMN output_examples TEXT;

-- verified: set to 1 once the verifier_url passes an automated quality check
ALTER TABLE agents ADD COLUMN verified INTEGER NOT NULL DEFAULT 0;

-- callback_secret: HMAC-SHA256 signing key for callback POST bodies
ALTER TABLE jobs ADD COLUMN callback_secret TEXT;
