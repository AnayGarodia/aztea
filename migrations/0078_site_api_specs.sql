-- 0078_site_api_specs.sql
--
-- "Compile a site into an API": a discovered JSON endpoint that backs rendered
-- data, so future navigations skip the browser entirely. Separate table from
-- site_maps because the replay path (direct HTTP, re-validated SSRF at replay),
-- the validation signal (response shape, not DOM), and the rot lifecycle all
-- differ. SECURITY (plan amendment C3): host + scheme + port are SIGNED and
-- NON-TEMPLATABLE so a stored spec can never be coerced into an SSRF gadget.
-- Only path/query may carry typed, bounded template params.

CREATE TABLE IF NOT EXISTS site_api_specs (
    api_spec_id          TEXT PRIMARY KEY,          -- 'sapi_' + 22 base62
    site_key             TEXT NOT NULL,
    map_id               TEXT,                       -- logical FK to site_maps.map_id
    author_did           TEXT NOT NULL,
    author_agent_id      TEXT NOT NULL,
    author_owner_id      TEXT NOT NULL,
    method               TEXT NOT NULL DEFAULT 'GET' CHECK (method IN ('GET','POST')),
    -- Immutable, signed network identity. Never templated.
    endpoint_scheme      TEXT NOT NULL DEFAULT 'https' CHECK (endpoint_scheme IN ('http','https')),
    endpoint_host        TEXT NOT NULL,
    endpoint_port        INTEGER,
    -- Only these may carry {param} placeholders, each typed in param_schema.
    path_template        TEXT NOT NULL,
    query_template       TEXT NOT NULL DEFAULT '',
    param_schema         TEXT NOT NULL DEFAULT '{}', -- JSON: typed, length-bounded params
    response_fingerprint TEXT NOT NULL,              -- SHA256 of validated JSON shape (keys/types)
    field_map            TEXT NOT NULL,              -- JSON: goal-field -> JSONPath
    signature            TEXT NOT NULL,
    signature_alg        TEXT NOT NULL DEFAULT 'Ed25519+aztea-sitemap-sig/1',
    status               TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','challenged','revoked','superseded')),
    hit_count            INTEGER NOT NULL DEFAULT 0,
    drift_count          INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    last_used_at         TEXT,
    last_validated_at    TEXT,
    revoked_at           TEXT
);

CREATE INDEX IF NOT EXISTS idx_site_api_specs_lookup
    ON site_api_specs(site_key, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_api_specs_author
    ON site_api_specs(author_agent_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_site_api_specs_endpoint
    ON site_api_specs(site_key, method, endpoint_host, path_template) WHERE status = 'active';
