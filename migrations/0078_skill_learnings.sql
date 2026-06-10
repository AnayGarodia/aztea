-- 0078_skill_learnings.sql
--
-- Self-improving hosted skills (learnings memory, Hermes-style). The
-- distiller turns a skill's accumulated failures into short corrective
-- "learning" bullets. The owner approves them, and approved bullets are injected
-- as a delimited DATA block at execution time (core/skill_executor.py). The
-- stored system_prompt is never mutated — reversal is a status flip to
-- 'archived'. Status transitions ARE the version history (append-only intent,
-- no hard deletes in the normal flow).
--
-- No DB-level FK on skill_id by design: hosted_skills already cascades from
-- agents ON DELETE CASCADE, so a RESTRICT-ing FK here would block that
-- cascade on Postgres. Orphan cleanup is handled app-side (the DELETE /skills
-- route archives a skill's learnings in the same transaction). See
-- core/skill_learnings.py and CLAUDE.md "do not rely on DB cascade".

CREATE TABLE IF NOT EXISTS skill_learnings (
    learning_id    TEXT PRIMARY KEY,
    skill_id       TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    owner_id       TEXT NOT NULL,
    text           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'proposed',
    source_signal  TEXT NOT NULL,
    source_job_ids TEXT,
    confidence     REAL,
    created_at     TEXT NOT NULL,
    decided_at     TEXT,
    decided_by     TEXT
);

CREATE INDEX IF NOT EXISTS idx_skill_learnings_skill ON skill_learnings(skill_id, status);
CREATE INDEX IF NOT EXISTS idx_skill_learnings_owner ON skill_learnings(owner_id, status);

-- Watermark: single source for "signal newer than the last distillation run".
-- NULL means "never distilled" (sweeper treats all current signal as new).
ALTER TABLE hosted_skills ADD COLUMN last_distill_at TEXT;
