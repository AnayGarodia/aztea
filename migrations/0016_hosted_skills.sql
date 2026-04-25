-- 0016_hosted_skills.sql
-- Hosted skills: OpenClaw-style SKILL.md files executed by Aztea on behalf of
-- the skill builder. One row per agent whose endpoint_url is "skill://{skill_id}".

CREATE TABLE IF NOT EXISTS hosted_skills (
    skill_id              TEXT PRIMARY KEY,
    agent_id              TEXT NOT NULL UNIQUE REFERENCES agents(agent_id) ON DELETE CASCADE,
    owner_id              TEXT NOT NULL,
    slug                  TEXT NOT NULL,
    raw_md                TEXT NOT NULL,
    system_prompt         TEXT NOT NULL,
    parsed_metadata_json  TEXT NOT NULL DEFAULT '{}',
    model_chain           TEXT,
    temperature           REAL NOT NULL DEFAULT 0.2 CHECK(temperature >= 0 AND temperature <= 2),
    max_output_tokens     INTEGER NOT NULL DEFAULT 1500 CHECK(max_output_tokens > 0 AND max_output_tokens <= 4000),
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hosted_skills_owner ON hosted_skills(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hosted_skills_agent ON hosted_skills(agent_id);
