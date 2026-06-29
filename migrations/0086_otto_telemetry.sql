-- 0086_otto_telemetry.sql
--
-- Anonymous product telemetry from the Otto macOS app (and the website download
-- redirect). One append-only row per event. event_id is the client-generated
-- UUID and is UNIQUE, so retried / offline-queued sends dedup instead of
-- double-counting. props holds the full event-specific JSON (stored as TEXT for
-- SQLite/Postgres parity). The hot columns below are denormalized copies of
-- frequently-sliced task fields so the dashboard aggregations never have to
-- parse JSON, which keeps the metrics queries identical and fast on both
-- backends (no JSONB or materialized-view dependency).
--
-- Privacy: no raw task text, no file names, no argument values ever land here.
-- device_id is an anonymous per-install UUID. See docs/otto-telemetry-schema.md.
--
-- NOTE: keep statement-terminator characters out of these comment lines — the
-- migration runner splits the file on each one before executing.

CREATE TABLE IF NOT EXISTS otto_telemetry_events (
    event_id        TEXT PRIMARY KEY,
    event           TEXT NOT NULL
        CHECK (event IN ('install','launch','task','permission','onboarding','account','error','download')),
    schema_version  INTEGER NOT NULL DEFAULT 1,
    device_id       TEXT,
    session_id      TEXT,
    app_version     TEXT,
    os_version      TEXT,
    mac_model       TEXT,
    ts_client       TEXT,
    ts_server       TEXT NOT NULL,
    day             TEXT NOT NULL,
    props           TEXT NOT NULL DEFAULT '{}',
    intent_category TEXT,
    app             TEXT,
    outcome         TEXT,
    failure_reason  TEXT,
    summon          TEXT,
    from_recipe     INTEGER,
    total_ms        INTEGER,
    ttfa_ms         INTEGER,
    model_ms        INTEGER,
    perceive_ms     INTEGER,
    act_ms          INTEGER,
    verify_ms       INTEGER,
    vision_steps    INTEGER,
    step_count      INTEGER,
    cost_usd        REAL
);

CREATE INDEX IF NOT EXISTS idx_otto_tel_event_day  ON otto_telemetry_events(event, day);
CREATE INDEX IF NOT EXISTS idx_otto_tel_device     ON otto_telemetry_events(device_id);
CREATE INDEX IF NOT EXISTS idx_otto_tel_day        ON otto_telemetry_events(day);
CREATE INDEX IF NOT EXISTS idx_otto_tel_task_slice ON otto_telemetry_events(event, intent_category, app);
