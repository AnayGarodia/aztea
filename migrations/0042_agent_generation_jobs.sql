-- 0042_agent_generation_jobs.sql
--
-- Sibling table to `jobs` for the vibe-an-agent self-serve generation flow
-- (POST /agents/generate). Not reusing `jobs` because that table carries
-- ~47 columns specific to agent-call lifecycle (dispute window, output
-- verification, lease/heartbeat) that don't apply to generation work.
--
-- Lifecycle: queued → running → succeeded | failed (terminal).
-- Idempotency is enforced via UNIQUE(owner_id, idempotency_key) so that
-- re-submitting the same request returns the existing generation_job_id
-- instead of charging the caller twice.

CREATE TABLE IF NOT EXISTS agent_generation_jobs (
  generation_job_id TEXT PRIMARY KEY,
  owner_id          TEXT NOT NULL,
  idempotency_key   TEXT NOT NULL,
  status            TEXT NOT NULL,
  request_json      TEXT NOT NULL,
  result_json       TEXT,
  iterations        INTEGER NOT NULL DEFAULT 0,
  cost_cents        INTEGER NOT NULL DEFAULT 0,
  agent_id          TEXT,
  charge_tx_id      TEXT,
  error_code        TEXT,
  error_message     TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE(owner_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_gen_jobs_owner_created
  ON agent_generation_jobs(owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_gen_jobs_status
  ON agent_generation_jobs(status, created_at DESC);
