-- 2026-05-19 (B15): jobs.claim_deadline_at — auto-fail jobs that never
-- get claimed by any worker.
--
-- Pre-fix, a job submitted for an agent whose workers were all offline
-- (or whose endpoint was misconfigured) sat in `pending` indefinitely.
-- Three real disputes shipped from agents that "accepted hires, opened
-- escrow, never executed" — the caller had no recourse short of a
-- cancel they had to discover existed.
--
-- New behavior: claim_deadline_at is set at INSERT to
-- created_at + AZTEA_JOB_CLAIM_DEADLINE_SECONDS (default 1800s = 30
-- minutes). The builtin-worker sweeper scans for jobs in `pending` past
-- their deadline, transitions them to `failed` with
-- error_message="agent.no_workers_claimed", and refunds the wallet
-- hold. Caller gets their money back and a clean signal that the
-- agent is unreachable.
--
-- Migration comments stay semicolon-free per the cluster A B-fix.

ALTER TABLE jobs ADD COLUMN claim_deadline_at TEXT;
