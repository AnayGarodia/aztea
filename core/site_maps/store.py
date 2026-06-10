"""DB read/write for the site-map commons. All access via core.db.

# OWNS: persistence + lifecycle of site_maps / site_map_usages / site_map_challenges.
#        Supersede-on-refresh, hit/health counters, and the consumer_job_id
#        idempotency anchor for royalties.
# NOT OWNS: signing (signing.py), ranking (ranking.py), money movement
#           (payouts.py), the live navigator/dispatch wiring, and site_api_specs
#           CRUD (table created by migration 0080; read/write lands with the
#           API-discovery sub-phase — the normalize/freshness helpers are ready).
# INVARIANTS:
#   * Exactly one 'active' row per (site_key, author_did) — enforced by the
#     partial unique index AND the supersede step inside put_map's transaction.
#   * record_usage is idempotent on consumer_job_id (one usage row per job).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from core import db as _db
from core.site_maps import signing

_LOG = logging.getLogger(__name__)

DB_PATH = _db.DB_PATH
_local = _db._local  # so tests/integration/helpers._close_module_conn works on this module

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ID_BYTES = 16  # 128 bits -> 22 base62 chars, matching the workspace id width


def _new_id(prefix: str) -> str:
    """Pure-ish: unguessable id ``<prefix>_<22 base62 chars>`` (~128 bits)."""
    n = int.from_bytes(secrets.token_bytes(_ID_BYTES), "big")
    chars: list[str] = []
    while n:
        n, rem = divmod(n, 62)
        chars.append(_BASE62[rem])
    body = "".join(reversed(chars)).rjust(22, "0")
    return f"{prefix}_{body}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(DB_PATH)


def init_site_maps_db() -> None:
    """Ensure commons tables exist by applying migrations — the single schema source.

    Why apply_migrations and not an inline CREATE TABLE: a second hand-maintained
    DDL copy drifts from migrations/0079-0081 (it silently lost the CHECK
    constraints and indexes), so tests would run against a weaker schema than
    prod. Running the migration runner against the configured path keeps one
    source of truth. Idempotent — already-applied versions are skipped.
    """
    from core.migrate import apply_migrations

    apply_migrations(DB_PATH)


def put_map(
    *, site_key: str, url_pattern: str, author_did: str, author_agent_id: str,
    author_owner_id: str, map_json: Any, dom_fingerprint: str, private_pem: str,
) -> dict[str, Any]:
    """Sign and insert a new active map version, superseding the author's prior active row.

    Version assignment, manifest signing, supersede, and insert all run inside one
    BEGIN IMMEDIATE transaction so (a) the signed version always equals the stored
    version, and (b) the partial-unique index (one active row per site_key+author)
    is never transiently violated even under a concurrent author for the same site.
    """
    map_id = _new_id("smap")
    now = _now()
    map_json_str = json.dumps(map_json, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    sha = signing.map_sha256(map_json)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM site_maps WHERE site_key = %s AND author_did = %s",
            (site_key, author_did),
        ).fetchone()
        version = int((prior or {}).get("v") or 0) + 1
        manifest = signing.build_map_manifest(
            site_key=site_key, url_pattern=url_pattern, map_json=map_json,
            dom_fingerprint=dom_fingerprint, author_did=author_did, version=version,
        )
        signature = signing.sign_map(private_pem, manifest)
        conn.execute(
            "UPDATE site_maps SET status = 'superseded' "
            "WHERE site_key = %s AND author_did = %s AND status = 'active'",
            (site_key, author_did),
        )
        conn.execute(
            """
            INSERT INTO site_maps (map_id, site_key, url_pattern, version, author_did,
                author_agent_id, author_owner_id, map_json, map_sha256, dom_fingerprint,
                signature, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
            """,
            (map_id, site_key, url_pattern, version, author_did, author_agent_id,
             author_owner_id, map_json_str, sha, dom_fingerprint, signature, now),
        )
    return get_map(map_id) or {}


def get_map(map_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        return conn.execute("SELECT * FROM site_maps WHERE map_id = %s", (map_id,)).fetchone()


def get_active_maps(site_key: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Active maps for a site_key, newest first (caller ranks)."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM site_maps WHERE site_key = %s AND status = 'active' "
            "ORDER BY created_at DESC LIMIT %s",
            (site_key, int(limit)),
        ).fetchall()


def bump_hit(map_id: str, *, fresh: bool) -> None:
    """Record a reuse: increment hit + the fresh/drift counter; stamp timestamps."""
    now = _now()
    with _conn() as conn:
        if fresh:
            conn.execute(
                "UPDATE site_maps SET hit_count = hit_count + 1, "
                "fresh_validation_count = fresh_validation_count + 1, "
                "last_used_at = %s, last_validated_at = %s WHERE map_id = %s",
                (now, now, map_id),
            )
        else:
            conn.execute(
                "UPDATE site_maps SET hit_count = hit_count + 1, "
                "drift_count = drift_count + 1, last_used_at = %s WHERE map_id = %s",
                (now, map_id),
            )


def revoke_map(map_id: str, *, reason: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE site_maps SET status = 'revoked', revoked_at = %s, revoked_reason = %s "
            "WHERE map_id = %s AND status != 'revoked'",
            (_now(), reason, map_id),
        )


def record_usage(
    *, map_id: str | None, api_spec_id: str | None, site_key: str, consumer_job_id: str,
    consumer_owner_id: str, author_owner_id: str, royalty_cents: int, validated_fresh: bool,
) -> dict[str, Any] | None:
    """Insert one usage row per consuming job. Returns the row, or None on a retry (conflict).

    The UNIQUE(consumer_job_id) index + ON CONFLICT DO NOTHING is the royalty
    idempotency anchor: a re-run of the same job finds the row and pays nothing.
    """
    usage_id = _new_id("smu")
    with _conn() as conn:
        result = conn.execute(
            """
            INSERT INTO site_map_usages (usage_id, map_id, api_spec_id, site_key, consumer_job_id,
                consumer_owner_id, author_owner_id, royalty_cents, validated_fresh, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(consumer_job_id) DO NOTHING
            """,
            (usage_id, map_id, api_spec_id, site_key, consumer_job_id, consumer_owner_id,
             author_owner_id, int(royalty_cents), 1 if validated_fresh else 0, _now()),
        )
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            return None  # conflict — job already recorded, idempotent no-op
        return conn.execute(
            "SELECT * FROM site_map_usages WHERE consumer_job_id = %s", (consumer_job_id,)
        ).fetchone()


def open_challenge_counts(site_key: str) -> dict[str, int]:
    """map_id -> count of open challenges, for ranking penalties."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT map_id, COUNT(*) AS n FROM site_map_challenges "
            "WHERE site_key = %s AND status = 'open' AND map_id IS NOT NULL GROUP BY map_id",
            (site_key,),
        ).fetchall()
    return {str(r["map_id"]): int(r["n"]) for r in rows}


# --------------------------------------------------------------------------- API specs
def put_api_spec(
    *, site_key: str, map_id: str | None, author_did: str, author_agent_id: str,
    author_owner_id: str, method: str, endpoint_scheme: str, endpoint_host: str,
    endpoint_port: int | None, path_template: str, query_template: str,
    param_schema: Any, response_fingerprint: str, field_map: Any, private_pem: str,
) -> dict[str, Any]:
    """Sign and insert a new active API spec, superseding the prior active row for the
    same (site_key, method, endpoint_host, path_template) — the partial unique key.

    Supersede + insert run in one BEGIN IMMEDIATE transaction so the partial-unique
    index is never transiently violated. The signed manifest binds the immutable
    scheme/host/port so a stored spec can never be coerced into a different endpoint
    (the SSRF firewall). Signing needs no DB read (no version column), so it runs
    before the transaction.
    """
    api_spec_id = _new_id("sapi")
    now = _now()
    param_schema_str = json.dumps(param_schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    field_map_str = json.dumps(field_map, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest = signing.build_api_spec_manifest(
        site_key=site_key, author_did=author_did, method=method,
        endpoint_scheme=endpoint_scheme, endpoint_host=endpoint_host,
        endpoint_port=endpoint_port, path_template=path_template,
        query_template=query_template, response_fingerprint=response_fingerprint,
        field_map=field_map, param_schema=param_schema,
    )
    signature = signing.sign_api_spec(private_pem, manifest)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE site_api_specs SET status = 'superseded' "
            "WHERE site_key = %s AND method = %s AND endpoint_host = %s "
            "AND path_template = %s AND status = 'active'",
            (site_key, method, endpoint_host, path_template),
        )
        conn.execute(
            """
            INSERT INTO site_api_specs (api_spec_id, site_key, map_id, author_did,
                author_agent_id, author_owner_id, method, endpoint_scheme, endpoint_host,
                endpoint_port, path_template, query_template, param_schema,
                response_fingerprint, field_map, signature, signature_alg, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
            """,
            (api_spec_id, site_key, map_id, author_did, author_agent_id, author_owner_id,
             method, endpoint_scheme, endpoint_host, endpoint_port, path_template,
             query_template, param_schema_str, response_fingerprint, field_map_str,
             signature, signing.API_SPEC_SIG_SCHEME, now),
        )
    return get_api_spec(api_spec_id) or {}


def get_api_spec(api_spec_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM site_api_specs WHERE api_spec_id = %s", (api_spec_id,)
        ).fetchone()


def get_active_api_specs(
    site_key: str, *, method: str | None = None, limit: int = 8,
) -> list[dict[str, Any]]:
    """Active API specs for a site_key, newest first (caller ranks). Optional method filter."""
    with _conn() as conn:
        if method is not None:
            return conn.execute(
                "SELECT * FROM site_api_specs WHERE site_key = %s AND method = %s "
                "AND status = 'active' ORDER BY created_at DESC LIMIT %s",
                (site_key, method, int(limit)),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM site_api_specs WHERE site_key = %s AND status = 'active' "
            "ORDER BY created_at DESC LIMIT %s",
            (site_key, int(limit)),
        ).fetchall()


def bump_api_spec_hit(api_spec_id: str, *, fresh: bool) -> None:
    """Record a replay: hit_count++ and stamp last_used_at; on fresh stamp
    last_validated_at, on drift increment drift_count.

    site_api_specs has no fresh_validation_count column (unlike site_maps), so
    reliability is derived downstream as (hit_count - drift_count).
    """
    now = _now()
    with _conn() as conn:
        if fresh:
            conn.execute(
                "UPDATE site_api_specs SET hit_count = hit_count + 1, "
                "last_used_at = %s, last_validated_at = %s WHERE api_spec_id = %s",
                (now, now, api_spec_id),
            )
        else:
            conn.execute(
                "UPDATE site_api_specs SET hit_count = hit_count + 1, "
                "drift_count = drift_count + 1, last_used_at = %s WHERE api_spec_id = %s",
                (now, api_spec_id),
            )


def revoke_api_spec(api_spec_id: str, *, reason: str) -> None:
    """Mark a spec revoked. site_api_specs has no revoked_reason column, so the
    reason is logged at this boundary (the challenge row, when present, persists it).
    """
    _LOG.info("revoking api_spec %s: %s", api_spec_id, reason)
    with _conn() as conn:
        conn.execute(
            "UPDATE site_api_specs SET status = 'revoked', revoked_at = %s "
            "WHERE api_spec_id = %s AND status != 'revoked'",
            (_now(), api_spec_id),
        )


def set_usage_royalty_tx(usage_id: str, royalty_tx_id: str) -> None:
    """Back-write the royalty ledger tx id onto a usage row, AFTER the credit lands.

    A usage row whose royalty_tx_id is still NULL is the reconcile sweep's signal that
    the author was claimed-but-not-yet-paid (a crash between claim and credit), so it
    can retry — the safe, never-double-pay failure direction for the royalty path.
    """
    with _conn() as conn:
        conn.execute(
            "UPDATE site_map_usages SET royalty_tx_id = %s WHERE usage_id = %s",
            (royalty_tx_id, usage_id),
        )
