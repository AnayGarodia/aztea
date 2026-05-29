-- 0067_stability_auto_flip.sql
-- 2026-05-28 C2 sweeper-driven override for stability_tier. Scoring path
-- reads stability_override (when non-NULL) and falls back to the agent
-- spec value otherwise. The flip-history table is append-only audit.
-- See core/registry/stability_monitor.py for the policy.

ALTER TABLE agents ADD COLUMN stability_override TEXT;

CREATE TABLE IF NOT EXISTS stability_flip_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    from_tier     TEXT,
    to_tier       TEXT NOT NULL,
    reason        TEXT NOT NULL,
    error_rate    REAL,
    window_size   INTEGER,
    actor         TEXT NOT NULL DEFAULT 'system:stability_monitor',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stability_flip_history_agent_created
  ON stability_flip_history(agent_id, created_at DESC);
