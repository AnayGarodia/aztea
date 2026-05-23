-- 0065_vector_store_and_repo_index.sql
-- 2026-05-22: foundation for the org-memory agent family (D16 Codebase
-- Reviewer first, with D17 D18 D19 D20 reusing the same surface).
--
-- vector_entries is the generic key-value-with-embedding table backing
-- the new core.vector_store module. Namespace partitions vectors per
-- consumer (repo:UUID for hosted_index, package:UUID for the deferred
-- E21 indexer). entry_id is unique within a namespace. embedding is
-- BLOB on SQLite, BYTEA on Postgres (the migrate.py adapter swaps the
-- type). On Postgres clusters that have the pgvector extension
-- available, core/_vector_pg.py promotes the column to VECTOR(384) and
-- creates an ivfflat index at module load — purely opt-in, no
-- migration-time dependency on the extension.
--
-- repo_index, repo_commits, repo_hunks, repo_incidents back the
-- core.hosted_index module. repo_hunks.vector_entry_id is a logical
-- foreign key into vector_entries.entry_id under the namespace
-- repo:repo_id — not enforced at DB level so the vector_store can
-- evict aggressively without cascading to the hunk metadata.
--
-- NOTE: keep migration comments free of semicolons (migrate.py
-- _split_statements splits naively on every semicolon).

CREATE TABLE IF NOT EXISTS vector_entries (
    namespace    TEXT NOT NULL,
    entry_id     TEXT NOT NULL,
    embedding    BLOB,
    metadata     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (namespace, entry_id)
);

CREATE INDEX IF NOT EXISTS vector_entries_namespace
    ON vector_entries(namespace, created_at DESC);

CREATE TABLE IF NOT EXISTS repo_index (
    repo_id            TEXT PRIMARY KEY,
    owner_id           TEXT NOT NULL,
    url                TEXT NOT NULL,
    last_ingested_at   TEXT,
    head_sha           TEXT,
    status             TEXT NOT NULL DEFAULT 'pending',
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS repo_index_owner
    ON repo_index(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS repo_commits (
    commit_sha     TEXT NOT NULL,
    repo_id        TEXT NOT NULL,
    parent_sha     TEXT,
    author         TEXT,
    ts             TEXT,
    was_reverted   INTEGER NOT NULL DEFAULT 0,
    hotfix_for     TEXT,
    PRIMARY KEY (repo_id, commit_sha)
);

CREATE INDEX IF NOT EXISTS repo_commits_hotfix
    ON repo_commits(repo_id, hotfix_for);

CREATE INDEX IF NOT EXISTS repo_commits_ts
    ON repo_commits(repo_id, ts DESC);

CREATE TABLE IF NOT EXISTS repo_hunks (
    hunk_id          TEXT PRIMARY KEY,
    commit_sha       TEXT NOT NULL,
    repo_id          TEXT NOT NULL,
    file             TEXT NOT NULL,
    ast_shape_hash   TEXT,
    vector_entry_id  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS repo_hunks_commit
    ON repo_hunks(repo_id, commit_sha);

CREATE INDEX IF NOT EXISTS repo_hunks_file
    ON repo_hunks(repo_id, file);

CREATE TABLE IF NOT EXISTS repo_incidents (
    incident_id      TEXT PRIMARY KEY,
    repo_id          TEXT NOT NULL,
    ts               TEXT,
    summary          TEXT,
    linked_commits   TEXT
);

CREATE INDEX IF NOT EXISTS repo_incidents_repo
    ON repo_incidents(repo_id, ts DESC);
