-- Wipe historical work-examples for security-category agents that may
-- have been recorded before they were correctly tagged. Inputs to these
-- agents include private dependency manifests and CVE queries that
-- should never be replayed to other buyers.
UPDATE agents SET output_examples = NULL
WHERE agent_id IN (
    '11fab82a-426e-513e-abf3-528d99ef2b87',  -- Dependency Auditor
    'a3e239dd-ea92-556b-9c95-0a213a3daf59'   -- CVE Lookup
);
