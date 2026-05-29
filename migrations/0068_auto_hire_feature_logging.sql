-- 0068_auto_hire_feature_logging.sql
-- 2026-05-28 Phase 3.5 forward-only feature logging for the future
-- learned ranker. feature_vector_json captures the per-candidate score
-- breakdown at decision time so Phase 4 has training data 30+ days
-- after this column ships. shadow_chosen_agent_id is reserved for the
-- learned-ranker shadow-mode rollout in Phase 4 NULL when both
-- rankers agreed or when the shadow ranker has not started.
-- intent_class is the Phase 2 classifier output NULL pre-Phase-2.

ALTER TABLE auto_hire_decisions ADD COLUMN feature_vector_json TEXT;
ALTER TABLE auto_hire_decisions ADD COLUMN shadow_chosen_agent_id TEXT;
ALTER TABLE auto_hire_decisions ADD COLUMN intent_class TEXT;

CREATE INDEX IF NOT EXISTS idx_auto_hire_decisions_intent_class
  ON auto_hire_decisions(intent_class, created_at DESC);
