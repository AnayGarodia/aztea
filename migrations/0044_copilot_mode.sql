-- 0044_copilot_mode.sql
--
-- Co-Pilot Mode: steerable calls, stop_when predicates, and signed
-- transcript receipts. See docs/superpowers/specs/2026-05-09-copilot-mode-design.md.
--
-- Adds columns to `jobs` for stop_when predicate state, partial_output /
-- steer counters, billing-unit hint, terminal-transition stamps, and the
-- final JWS receipt. Adds a generalized `pending_settlements` queue that
-- decouples ledger settlement + receipt-signing from the messaging
-- transaction (and which the existing complete/failed/cancelled paths
-- migrate onto in this same change).
--
-- billing_unit is enforced via pydantic Literal at the API boundary
-- (Literal['call','partial']). No CHECK constraint here because SQLite
-- ALTER TABLE ADD COLUMN handling of CHECK varies across versions.

ALTER TABLE jobs ADD COLUMN stop_when_json TEXT;
ALTER TABLE jobs ADD COLUMN stop_reason_json TEXT;
ALTER TABLE jobs ADD COLUMN partials_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN steer_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN billing_unit TEXT;
ALTER TABLE jobs ADD COLUMN receipt_jws TEXT;
ALTER TABLE jobs ADD COLUMN terminal_at TEXT;
ALTER TABLE jobs ADD COLUMN terminal_message_id INTEGER;

CREATE TABLE IF NOT EXISTS pending_settlements (
    job_id            TEXT PRIMARY KEY,
    terminal_state    TEXT NOT NULL,
    terminal_at       TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    settled_at        TEXT,
    receipt_built_at  TEXT,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pending_settlements_unsettled
    ON pending_settlements(settled_at);
