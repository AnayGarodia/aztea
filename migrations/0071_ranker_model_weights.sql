-- 0071_ranker_model_weights.sql
-- 2026-05-28 Phase 4 storage for learned-ranker model weights and
-- calibration parameters. Active version is selected by the
-- auto_invoke_use_learned_ranker feature flag plus the active_version
-- column in ranker_model_active. Rollback is a flag-flip + version-
-- pointer change NOT a column drop (per /autoplan E-10 SQLite ALTER
-- TABLE DROP COLUMN safety).

CREATE TABLE IF NOT EXISTS ranker_model_weights (
    version              TEXT PRIMARY KEY,
    weights_json         TEXT NOT NULL,
    calibration_json     TEXT,
    feature_names_json   TEXT NOT NULL,
    trained_at           TEXT NOT NULL,
    training_window_days INTEGER,
    n_training_rows      INTEGER,
    notes                TEXT
);

CREATE TABLE IF NOT EXISTS ranker_model_active (
    singleton_key        INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton_key = 1),
    active_version       TEXT REFERENCES ranker_model_weights(version),
    activated_at         TEXT NOT NULL,
    activated_by         TEXT NOT NULL DEFAULT 'system'
);
