"""Pure normalization + fingerprinting for the site-map commons.

# OWNS: the canonical site_key, url pattern, and the two fingerprints (DOM
#        structural + API response shape) that every other commons module keys on.
# NOT OWNS: DB access (store.py), signing (signing.py), ranking (ranking.py).
# INVARIANTS:
#   * normalize_site_key is idempotent: normalize(normalize(u)) == normalize(u).
#   * dom_fingerprint is value-stripped — it hashes the role skeleton, never
#     page text/values, so it drifts on structure (what breaks selectors) and
#     carries no PII into the shared commons.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlparse

# Tracking params carry no routing meaning; dropping them keeps one logical
# page from fragmenting into many site_keys.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_eid", "ref", "ref_src",
    "igshid", "_hsenc", "_hsmi", "yclid", "spm",
})
# Path segments that are identifiers (numeric, uuid, long hex) collapse to '*'
# so /item/123 and /item/456 share a site_key.
_NUMERIC_RE = re.compile(r"^\d+$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_LONG_HEX_RE = re.compile(r"^[0-9a-fA-F]{16,}$")


def _collapse_segment(segment: str) -> str:
    """Pure: identifier-looking path segments become '*'."""
    if not segment:
        return segment
    if _NUMERIC_RE.match(segment) or _UUID_RE.match(segment) or _LONG_HEX_RE.match(segment):
        return "*"
    return segment.lower()


def _semantic_query_keys(query: str) -> list[str]:
    """Pure: sorted, de-duplicated non-tracking query keys (keys only, not values)."""
    keys = {
        k.lower()
        for k, _ in parse_qsl(query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    }
    return sorted(keys)


def _path_pattern(path: str) -> str:
    """Pure: collapse id segments and normalize trailing slash."""
    segments = [_collapse_segment(s) for s in path.split("/")]
    collapsed = "/".join(segments)
    if len(collapsed) > 1 and collapsed.endswith("/"):
        collapsed = collapsed.rstrip("/")
    return collapsed or "/"


def normalize_site_key(url: str) -> str:
    """Pure: stable key that both authoring and lookup hash on.

    Lowercase host (sans leading www.), id-collapsed path, and the sorted set of
    semantic query keys (not values). Idempotent.
    """
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    key = f"{host}{_path_pattern(parsed.path)}"
    semantic_keys = _semantic_query_keys(parsed.query)
    if semantic_keys:
        key = f"{key}?{','.join(semantic_keys)}"
    return key


def url_pattern_from(url: str) -> str:
    """Pure: human-readable glob form of the normalized key, e.g. 'site.com/item/*?id'."""
    return normalize_site_key(url)


def dom_fingerprint(normalized_url: str, roles: list[str]) -> str:
    """Pure: value-stripped structural fingerprint (the Stagehand-style replay anchor).

    Hashes the role skeleton + node count, never names/values, so it drifts on
    structural change while staying free of page content / PII.
    """
    skeleton = "|".join(str(r or "") for r in roles)
    payload = f"{normalized_url}\n{len(roles)}\n{skeleton}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _shape_pairs(obj: Any, path: str, out: list[str]) -> None:
    """Side-effect (accumulator): collect sorted (jsonpath, type) pairs of a JSON value."""
    if isinstance(obj, dict):
        out.append(f"{path}:object")
        for key in sorted(obj):
            _shape_pairs(obj[key], f"{path}.{key}", out)
    elif isinstance(obj, list):
        out.append(f"{path}:array")
        # Shape of the first element represents the array (homogeneous assumption).
        if obj:
            _shape_pairs(obj[0], f"{path}[]", out)
    else:
        out.append(f"{path}:{type(obj).__name__}")


def response_shape_fingerprint(json_obj: Any) -> str:
    """Pure: SHA256 over the JSON *shape* (keys/types), resilient to value changes.

    Used to validate a discovered API spec still returns the same structure
    before replaying it — sensitive to schema change, blind to data churn.
    """
    pairs: list[str] = []
    _shape_pairs(json_obj, "$", pairs)
    return hashlib.sha256("\n".join(sorted(pairs)).encode("utf-8")).hexdigest()


def parse_iso_to_epoch(ts: str | None) -> float | None:
    """Pure: ISO-8601 -> epoch seconds, or None when absent/unparseable.

    Naive (tz-less) input is treated as UTC so a backfilled or hand-edited row
    can't skew ranking/TTL by the host's local UTC offset. Single shared impl so
    ranking and freshness don't each carry (and each have to fix) a copy.
    """
    if not ts:
        return None
    try:
        normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
