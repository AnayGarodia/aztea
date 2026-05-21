"""Result-cache helpers for trusted agent outputs.

# OWNS: opt-out result cache for agent outputs keyed by (agent_id, request hash).
# NOT OWNS: trust-score computation (delegated to core.reputation), agent
#           pricing, or any settlement logic. Pure read/write surface.
# INVARIANTS:
#   * Endpoints in _NON_CACHEABLE_INTERNAL_ENDPOINTS must never be cached
#     (side-effecting agents like the python executor).
#   * TTL values are bounded and derived from trust score; the cache layer
#     never extends TTL beyond what reputation.py reports.
# DECISIONS:
#   * Trust-based TTL has a small staleness window: between the moment a
#     cache hit is read and the moment we re-check the trust score, the
#     score could decay below the cacheable threshold. We accept that
#     gap because the cache is a perf optimization, not a security gate.
#     A correctness-critical reader must re-validate trust at the call
#     site, not rely on the cache hit alone.
#   * Cache keys are SHA256 of the canonicalized JSON request — chosen
#     for stability across Python versions, not for crypto security.
# KNOWN DEBT: the trust check between cache lookup and return is not
#             atomic. Acceptable for non-critical reads. Re-evaluate if
#             we ever cache anything money-bearing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from core import db as _db
from core import reputation

_LOG = logging.getLogger(__name__)

DB_PATH = _db.DB_PATH
_local = _db._local
# 1.7.3 — python_executor was removed from this list. Pre-1.7.3 ten
# identical concurrent calls to python_executor produced ten distinct
# charges and ten distinct executions, because the agent was marked
# non-cacheable (assumed side-effecting). In practice the sandbox
# captures stdout/stderr deterministically and the canonical output is
# the captured text — replaying a cache hit returns exactly what a
# fresh run would produce. The result-cache key is SHA256 over agent +
# input, so a "send the same code 10 times" workflow rightly dedupes
# to one charge. Users who genuinely want 10 distinct runs (e.g.
# print(time.time())) should use distinct inputs.
#
# Shell executor stays out — it can interact with the host filesystem
# and network in ways the cache layer can't reason about. Multi-file
# executor stays out for the same reason: the multi-file mode invokes
# real subprocess builds that may produce externally-visible artifacts.
_NON_CACHEABLE_INTERNAL_ENDPOINTS = {
    "internal://shell_executor",
    "internal://multi_file_executor",
}


def _resolved_db_path() -> str:
    module = sys.modules.get("core.cache")
    if module is not None:
        candidate = getattr(module, "DB_PATH", None)
        if isinstance(candidate, str) and candidate and candidate != _db.DB_PATH:
            return candidate
    registry_module = sys.modules.get("core.registry")
    if registry_module is not None:
        candidate = getattr(registry_module, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return DB_PATH


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(_resolved_db_path())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def init_cache_db() -> None:
    """Create the ``agent_result_cache`` table and indexes if they do not exist. Idempotent."""
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_result_cache (
                cache_key     TEXT PRIMARY KEY,
                agent_id      TEXT NOT NULL,
                output_json   TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                job_id        TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_agent ON agent_result_cache(agent_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_expires ON agent_result_cache(expires_at)"
        )


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def cache_key(
    agent_id: str, input_payload: Any, version_token: str | None = None
) -> str:
    canonical = f"{str(agent_id).strip()}:{str(version_token or '').strip()}:{_canonical_json(input_payload)}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def agent_cacheable(agent: dict | None) -> bool:
    """Return True if the agent listing has result caching enabled.

    Caching is opt-out: all agents are cacheable by default unless the agent
    row explicitly sets ``cacheable=False`` or the endpoint is in the internal
    non-cacheable list (e.g. agents that have side-effects).
    """
    if not isinstance(agent, dict):
        return True
    explicit = agent.get("cacheable")
    if explicit is not None:
        return bool(explicit)
    endpoint = str(agent.get("endpoint_url") or "").strip().lower()
    if endpoint in _NON_CACHEABLE_INTERNAL_ENDPOINTS:
        return False
    return True


def cache_identity(agent: dict | None, agent_id: str | None = None) -> str:
    """Return a stable string that captures the agent's current version for cache keying.

    Encodes agent_id + endpoint_url + updated_at + reviewed_at so that any
    change to the agent listing automatically invalidates cached results.
    """
    if not isinstance(agent, dict):
        return str(agent_id or "").strip()
    normalized_agent_id = str(agent.get("agent_id") or agent_id or "").strip()
    endpoint = str(agent.get("endpoint_url") or "").strip()
    version_bits = [
        endpoint,
        str(agent.get("updated_at") or "").strip(),
        str(agent.get("reviewed_at") or "").strip(),
    ]
    return f"{normalized_agent_id}:{'|'.join(version_bits)}"


def _current_trust_score(agent_id: str) -> float:
    return float(reputation.compute_trust_metrics(agent_id).get("trust_score") or 0.0)


def get_cached(
    agent_id: str, input_payload: Any, *, version_token: str | None = None
) -> Any | None:
    """Look up a cached result for (agent_id, input_payload); returns None on miss or TTL expiry.

    Expired entries are deleted on read (lazy eviction). Returns the decoded
    output dict, or None if not found.
    """
    init_cache_db()
    key = cache_key(agent_id, input_payload, version_token)
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT output_json, expires_at
            FROM agent_result_cache
            WHERE cache_key = %s
            """,
            (key,),
        ).fetchone()
        if row is None:
            return None
        expires_at = str(row["expires_at"] or "").strip()
        if expires_at and expires_at <= _now().isoformat():
            conn.execute("DELETE FROM agent_result_cache WHERE cache_key = %s", (key,))
            return None
        try:
            return json.loads(row["output_json"])
        except (json.JSONDecodeError, TypeError):
            conn.execute("DELETE FROM agent_result_cache WHERE cache_key = %s", (key,))
            return None


_BUILTIN_CACHE_BYPASS_THRESHOLD: set[str] | None = None


def _is_builtin_agent(agent_id: str) -> bool:
    """Return True for agent IDs maintained by the platform itself.

    Builtin agents skip the trust-score gate because we control their behaviour
    directly; gating them by reputation just means caches stay empty until users
    rate them, which never happens organically. Imported lazily because cache.py
    is imported very early in app startup.
    """
    global _BUILTIN_CACHE_BYPASS_THRESHOLD
    if _BUILTIN_CACHE_BYPASS_THRESHOLD is None:
        try:
            from server.builtin_agents.constants import (
                BUILTIN_AGENT_IDS,
                CURATED_PUBLIC_BUILTIN_AGENT_IDS,
            )

            _BUILTIN_CACHE_BYPASS_THRESHOLD = set(
                CURATED_PUBLIC_BUILTIN_AGENT_IDS
            ) | set(BUILTIN_AGENT_IDS)
        except ImportError:
            _LOG.warning(
                "builtin agent IDs unavailable; cache bypass set empty",
                exc_info=True,
            )
            _BUILTIN_CACHE_BYPASS_THRESHOLD = set()
    return str(agent_id).strip() in _BUILTIN_CACHE_BYPASS_THRESHOLD


# WHY: trust_score is on a 0–100 scale (see core/reputation.py). 60.0 admits
# agents with a handful of positive ratings while excluding fresh / low-trust
# accounts; below this floor the cache is effectively dead for the agent.
_EXTERNAL_AGENT_CACHE_TRUST_THRESHOLD = 60.0


def set_cached(
    agent_id: str,
    input_payload: Any,
    output_payload: Any,
    job_id: str,
    ttl_hours: int = 24,
    *,
    version_token: str | None = None,
) -> bool:
    """Store an agent result in the cache with a TTL (default 24 h, max 168 h).

    Builtin agents are always cached (we control them). External agents are
    cached only when their trust score is at or above
    ``_EXTERNAL_AGENT_CACHE_TRUST_THRESHOLD``. Returns True if stored.
    """
    init_cache_db()
    if not _is_builtin_agent(agent_id):
        if _current_trust_score(agent_id) < _EXTERNAL_AGENT_CACHE_TRUST_THRESHOLD:
            return False
    ttl = max(1, min(int(ttl_hours or 24), 168))
    key = cache_key(agent_id, input_payload, version_token)
    created_at = _now()
    expires_at = created_at + timedelta(hours=ttl)
    payload_to_store = output_payload
    if isinstance(output_payload, dict):
        payload_to_store = dict(output_payload)
        payload_to_store["_cached_job_id"] = str(job_id).strip()
    output_json = json.dumps(
        payload_to_store, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agent_result_cache (cache_key, agent_id, output_json, created_at, expires_at, job_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(cache_key) DO UPDATE SET
                agent_id = excluded.agent_id,
                output_json = excluded.output_json,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                job_id = excluded.job_id
            """,
            (
                key,
                agent_id,
                output_json,
                created_at.isoformat(),
                expires_at.isoformat(),
                str(job_id).strip(),
            ),
        )
    return True


def evict_expired() -> int:
    init_cache_db()
    with _conn() as conn:
        result = conn.execute(
            "DELETE FROM agent_result_cache WHERE expires_at < %s",
            (_now().isoformat(),),
        )
    return int(result.rowcount or 0)
