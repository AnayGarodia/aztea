"""TTL cache for the public-integration manifest endpoints.

# OWNS: per-format snapshot + ETag of the *anonymous* OpenAI/Gemini tool
#       manifests served at ``GET /api/integrations/*-tools.json``.
# NOT OWNS: the authenticated ``/openai/tools`` / ``/gemini/tools`` cache
#       (that's ``_agents_list_cache`` in server/application_parts/part_007.py),
#       agent enrichment (core/reputation.py), or tool-manifest construction
#       (core/tool_adapters.py).
# INVARIANTS:
#   1. Must NOT import or share state with ``_agents_list_cache``. The
#      private cache is keyed implicitly on time only — an admin request
#      that pre-warmed it would otherwise leak ``include_unapproved=True``
#      rows to anonymous callers on the next 15s.
#   2. Cache key is the format string ONLY. Never include caller identity,
#      header values, or query params; the public manifest must be
#      identical for every anonymous IP, byte-for-byte.
#   3. ETag is the SHA-256 of the canonical-JSON-serialised manifest body.
#      Two calls that return identical JSON return identical ETags.
# DECISIONS:
#   * 60s TTL — matches the ``Cache-Control: public, max-age=60`` header
#     the route emits, so HTTP caches and the in-process cache age in lockstep.
#   * Module-level singleton (not a class) — single process, no test
#     isolation concerns; ``reset_for_tests()`` flushes everything.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Callable, Literal

PublicManifestFormat = Literal["openai_chat", "openai_responses", "gemini"]

# WHY: 60s mirrors the ``max-age=60`` Cache-Control header so a downstream
# HTTP cache doesn't outlive the in-process snapshot — staleness windows
# stay aligned.
_PUBLIC_MANIFEST_TTL_SECONDS = 60.0

_CacheEntry = tuple[dict, str, float]  # (manifest, etag, expires_at_monotonic)

_lock = threading.Lock()
_entries: dict[str, _CacheEntry] = {}


def _canonical_etag(manifest: dict) -> str:
    """Pure: stable double-quoted ETag for a manifest body.

    sort_keys + separators ensure the same dict structure produces the
    same bytes regardless of insertion order; a SHA-256 prefix is enough
    bits for cache validation and stays short on the wire.
    """
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f'"{digest[:32]}"'


def get_public_manifest(
    fmt: PublicManifestFormat,
    builder: Callable[[], dict],
) -> tuple[dict, str]:
    """Return ``(manifest, etag)`` for the public manifest of the given format.

    The builder is only invoked on miss. ``builder`` must be idempotent —
    it will be called under the cache lock, so it should not perform
    expensive I/O. Pass a lambda that does the manifest computation
    (read agents from the registry, scrub, build).
    """
    now = time.monotonic()
    with _lock:
        cached = _entries.get(fmt)
        if cached is not None:
            manifest, etag, expires_at = cached
            if now < expires_at:
                return manifest, etag
        manifest = builder()
        etag = _canonical_etag(manifest)
        _entries[fmt] = (manifest, etag, now + _PUBLIC_MANIFEST_TTL_SECONDS)
        return manifest, etag


def reset_for_tests() -> None:
    """Test-only: flush all cached entries. Production code must not call this."""
    with _lock:
        _entries.clear()
