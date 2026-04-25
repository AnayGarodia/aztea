-- Add kind discriminator to agent listings.
-- kind: 'aztea_built' | 'community_skill' | 'self_hosted'
ALTER TABLE agents ADD COLUMN kind TEXT NOT NULL DEFAULT 'self_hosted';

-- Backfill built-in agents
UPDATE agents SET kind = 'aztea_built' WHERE agent_id IN (
    'b7741251-d7ac-5423-b57d-8e12cd80885f',
    '8cea848f-a165-5d6c-b1a0-7d14fff77d14',
    '9a175aa2-8ffd-52f7-aae0-5a33fc88db83',
    'a3e239dd-ea92-556b-9c95-0a213a3daf59',
    '9cf0d9d0-4a10-58c9-b97a-6b5f81b1cf33',
    '4fb167bd-b474-5ea5-bd5c-8976dfe799ae',
    'c12994de-cde9-514a-9c07-a3833b25bb1f',
    '9e673f6e-9115-516f-b41b-5af8bcbf15bd',
    '040dc3f5-afe7-5db7-b253-4936090cc7af',
    '32cd7b5c-44d0-5259-bb02-1bbc612e92d7',
    '5896576f-bbe6-59e4-83c1-5106002e7d10',
    '31cc3a99-eca6-5202-96d4-8366f426ae1d',
    '3d677381-791c-5e83-8e66-5b77d0e43e2e'
);

-- Backfill community skills (agent rows created by skill uploads)
UPDATE agents SET kind = 'community_skill'
WHERE agent_id IN (SELECT agent_id FROM hosted_skills);

CREATE INDEX IF NOT EXISTS idx_agents_kind ON agents(kind);
