-- Remove legacy demo / smoke-test skill rows from the registry. These were
-- previously hidden by an in-memory blocklist; the blocklist has been deleted
-- and the rows themselves are now dropped so the surface is the same on every
-- read path.
DELETE FROM agents
WHERE LOWER(TRIM(COALESCE(name, ''))) IN (
    'reverse_string', 'reverse string',
    'echo_skill', 'echo skill',
    'json_validator', 'json validator'
);
