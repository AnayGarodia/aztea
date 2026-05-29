-- 0069_intent_class_success_rollup.sql
-- 2026-05-28 Phase 3 per-agent per-intent-class daily rollup. Computed
-- by core/observability.py::run_decision_retention as a side-effect of
-- the existing 24h sweeper pass. Keyed on day + agent_id + intent_class
-- intent_class NULL rows aggregate under unclassified.

CREATE TABLE IF NOT EXISTS agent_intent_class_success_daily (
    day                   TEXT NOT NULL,
    agent_id              TEXT NOT NULL,
    intent_class          TEXT NOT NULL DEFAULT 'unclassified',
    n_decisions           INTEGER NOT NULL DEFAULT 0,
    n_success_4plus       INTEGER NOT NULL DEFAULT 0,
    n_success_5star       INTEGER NOT NULL DEFAULT 0,
    computed_at           TEXT NOT NULL,
    PRIMARY KEY (day, agent_id, intent_class)
);

CREATE INDEX IF NOT EXISTS idx_intent_class_success_agent_class
  ON agent_intent_class_success_daily(agent_id, intent_class, day DESC);
