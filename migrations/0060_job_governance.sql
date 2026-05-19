-- 2026-05-19: trust-rail governance columns.
--
-- jobs.per_job_cap_cents (B1) — caller-supplied hard ceiling on a single
-- job's charge. Combines with the API-key-level cap via MIN. The gate
-- fires BEFORE wallet hold so no refund is needed when it trips. NULL
-- means no per-job override and the API-key cap (if any) still applies.
--
-- wallets.session_budget_cents + session_budget_set_at (B3) — server-side
-- session budget. Sums charges since session_budget_set_at. When set,
-- pre_call_charge raises wallet.session_budget_exceeded if a new charge
-- would push the total past the cap. Replaces the prior client-side-only
-- MCP gate that was bypassed by any HTTP caller or process restart.
--
-- Migrations run exactly once per database (tracked via schema_migrations).
-- Bare ADD COLUMN is safe and matches the existing migration style.
-- NOTE: keep migration comments free of semicolons. The migrate.py
-- _split_statements helper splits on every semicolon, including ones
-- inside comments, which produces phantom statement fragments.

ALTER TABLE jobs ADD COLUMN per_job_cap_cents INTEGER;

ALTER TABLE wallets ADD COLUMN session_budget_cents INTEGER;
ALTER TABLE wallets ADD COLUMN session_budget_set_at TEXT;
