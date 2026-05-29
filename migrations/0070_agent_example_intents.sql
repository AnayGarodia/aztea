-- 0070_agent_example_intents.sql
-- 2026-05-28 Phase 2 (B1) per-agent canonical example intents used by
-- the routing semantic-similarity helper. Populated by core/registry/
-- example_intents.py at agent registration (async, never blocks the
-- registration response). Both LLM-generated and operator-curated
-- entries land here. Embedded lazily on first scoring use.

CREATE TABLE IF NOT EXISTS agent_example_intents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    intent_text   TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'generated',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_example_intents_agent
  ON agent_example_intents(agent_id);
