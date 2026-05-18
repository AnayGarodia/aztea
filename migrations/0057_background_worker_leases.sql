-- 0057_background_worker_leases.sql
-- DB-backed lease for background workers (dispute judge, sweeper, etc.)
-- so leadership can be re-acquired after a worker restart without an
-- operator intervention. Replaces the boot-once fcntl lock for the
-- dispute judge in particular: a worker that died between boot and the
-- next judge tick used to leave disputes wedged indefinitely because
-- the surviving worker never became leader.
--
-- One row per worker kind (e.g. dispute_judge, sweeper). A live lease
-- has expires_at in the future. Any worker can take it once expires_at
-- has passed. Heartbeat advances expires_at.
--
-- Idempotency for the judge writes is enforced by a unique index on
-- dispute_judgments(dispute_id, judge_kind) added in this migration.
-- If a brief two-leader window writes the same vote twice, the second
-- insert fails the constraint and the dispute resolution stays clean.

CREATE TABLE IF NOT EXISTS background_worker_leases (
    kind         TEXT PRIMARY KEY,
    holder_id    TEXT NOT NULL,
    hostname     TEXT,
    pid          INTEGER,
    acquired_at  TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS background_worker_leases_expires_idx
    ON background_worker_leases(expires_at);

-- Idempotency guard for the dispute judge so a transient two-leader
-- overlap can't double-vote the same dispute. Without this, the
-- original boot-once fcntl design assumed exactly-once write semantics
-- that the new DB lease cannot guarantee on its own.
CREATE UNIQUE INDEX IF NOT EXISTS dispute_judgments_dispute_judge_uq
    ON dispute_judgments(dispute_id, judge_kind);
